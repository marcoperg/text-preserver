from __future__ import annotations

from contextlib import closing, redirect_stdout
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from text_preserver.access.catalogue import (
    _extract_representation,
    _render_item_facets,
    _validate_database,
    build_catalogue,
    current_catalogue_index,
    search_catalogue,
)
from text_preserver.access.reader import READER_SCHEMA_VERSION, _canonical_json
from text_preserver.access.reader import _validate_streamed_reader_tree
from text_preserver.access.reader_model import (
    AccessCollection,
    AccessArtifact,
    AccessFacet,
    AccessItem,
    AccessRepresentation,
    access_document,
    access_id,
    access_json,
)
from text_preserver.cli import main
from text_preserver.config import load_config
from text_preserver.derived import AnalysisError


class CatalogueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.config_path = self.root / "collections.toml"
        self.config_path.write_text(
            """
[project]
archive_root = "./data/archive"
derived_root = "./data/derived"
workspace_root = "./data/workspace"
operator = "Test operator"
contact = "mailto:test@example.org"
user_agent = "text-preserver-test/1.0"

[[collections]]
id = "alpha"
title = "Alpha Collection"
description = "First collection"
rights_note = "Alpha rights"

[[collections.sources]]
id = "source"
kind = "web"
title = "Source"
seeds = ["https://example.org/alpha"]
allowed_hosts = ["example.org"]

[[collections]]
id = "beta"
title = "Beta Collection"
description = "Second collection"
rights_note = "Beta rights"

[[collections.sources]]
id = "source"
kind = "web"
title = "Source"
seeds = ["https://example.org/beta"]
allowed_hosts = ["example.org"]
""".strip(),
            encoding="utf-8",
        )

    def create_reader(
        self,
        collection_id: str,
        *,
        body: str = "Divine kingship and sacred river.",
        language: str = "en",
        kind: str = "translation",
        count: int = 1,
        schema_one: bool = False,
    ) -> Path:
        derived = self.root / "data/derived"
        capture_id = "20260829T120000Z-a1b2c3"
        build_inputs = {"fixture": collection_id}
        build_key = hashlib.sha256(_canonical_json(build_inputs)).hexdigest()
        collection_root = derived / "collections" / collection_id
        generation = (
            collection_root
            / f"captures/{capture_id}/reader-generations/{build_key}"
        )
        (generation / "works").mkdir(parents=True)
        artifact_id = access_id(collection_id, "artifact", "source.xml")
        items = []
        index_links = []
        for number in range(1, count + 1):
            work = f"work-{number}"
            item_id = access_id(collection_id, "item", work)
            representation_id = access_id(
                collection_id, "representation", f"{work}/main"
            )
            route = f"works/{work}.html"
            label = "Work One" if count == 1 else f"Work {number}"
            items.append(
                AccessItem(
                    item_id,
                    label,
                    "work",
                    route,
                    f"{collection_id.title()}, {label}.",
                    (
                        AccessRepresentation(
                            representation_id,
                            "Main text",
                            kind,
                            language,
                            f"{route}#representation-main",
                            (artifact_id,),
                        ),
                    ),
                    facets=(
                        AccessFacet(
                            "category",
                            "Source category",
                            ("Narrative literature",),
                            note="Source categories are provisional.",
                        ),
                    ),
                )
            )
            index_links.append(f'<a href="{route}">Work {number}</a>')
            searchable_body = body if count == 1 else f"{body} needle{number}"
            (generation / route).write_text(
                "<!doctype html><html><head><style>hidden style token</style></head><body>"
                '<nav>navigation token</nav><article id="representation-main">'
                f'<p>{searchable_body}</p><aside class="reader-citation">citation noise</aside>'
                '<script>script noise</script></article><footer>footer token</footer></body></html>',
                encoding="utf-8",
            )
        graph = AccessCollection(
            access_id(collection_id, "collection", ""),
            f"{collection_id.title()} Collection",
            "complete",
            "index.html",
            tuple(items),
            (
                AccessArtifact(
                    artifact_id,
                    "Source XML",
                    "preservation_original",
                    "sources/source/mirror/source.xml",
                    "application/xml",
                ),
            ),
        )
        if schema_one:
            document = access_document(graph)
            document["schema_version"] = 1
            for item in document["items"]:
                item.pop("facets")
            access_source = json.dumps(document, indent=2, sort_keys=True) + "\n"
        else:
            access_source = access_json(graph)
        (generation / "access.json").write_text(access_source, encoding="utf-8")
        (generation / "index.html").write_text(
            f"<!doctype html><html><body>{''.join(index_links)}</body></html>",
            encoding="utf-8",
        )
        output_tree = _validate_streamed_reader_tree(generation)
        metadata = {
            "schema_version": READER_SCHEMA_VERSION,
            "status": "complete",
            "collection_id": collection_id,
            "capture_id": capture_id,
            "build_key": build_key,
            "build_inputs": build_inputs,
            "output_tree": output_tree,
        }
        (generation / "metadata.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        collection_root.mkdir(parents=True, exist_ok=True)
        (collection_root / "LATEST-READER").write_text(
            f"captures/{capture_id}/reader-generations/{build_key}\n",
            encoding="utf-8",
        )
        os.symlink(
            os.path.relpath(generation, collection_root),
            collection_root / "reader",
            target_is_directory=True,
        )
        return generation

    def test_builds_catalogue_and_searches_representation_bodies(self) -> None:
        alpha = self.create_reader("alpha")
        config = load_config(self.config_path)

        first = build_catalogue(config)
        first.index_path.chmod(0o644)
        second = build_catalogue(config)

        self.assertEqual(first.metadata["status"], "complete_with_warnings")
        self.assertEqual(first.metadata["build_key"], second.metadata["build_key"])
        self.assertEqual(first.output_directory, second.output_directory)
        self.assertEqual(second.index_path.stat().st_mode & 0o222, 0)
        self.assertTrue(first.current_catalogue_updated)
        self.assertEqual(current_catalogue_index(config), first.index_path)
        catalogue = json.loads((first.output_directory / "catalogue.json").read_text())
        self.assertEqual([value["configured_id"] for value in catalogue["collections"]], ["alpha", "beta"])
        self.assertEqual(catalogue["collections"][1]["access_state"], "unavailable")
        self.assertEqual(catalogue["collections"][0]["artifacts"][0]["label"], "Source XML")
        self.assertEqual(
            catalogue["collections"][0]["items"][0]["facets"][0]["values"],
            ["Narrative literature"],
        )
        self.assertEqual(
            catalogue["collections"][0]["items"][0]["representations"][0]["artifact_ids"],
            ["tp:alpha/artifact/source.xml"],
        )
        with closing(sqlite3.connect(first.database_path)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM artifacts").fetchone()[0], 1)
            self.assertEqual(
                connection.execute("SELECT count(*) FROM representation_artifacts").fetchone()[0],
                1,
            )
        page = first.index_path.read_text(encoding="utf-8")
        self.assertIn("Work One", page)
        self.assertIn("Source category", page)
        self.assertIn("Narrative literature", page)
        self.assertIn("Source categories are provisional.", page)
        self.assertIn("../../collections/alpha/captures/", page)
        self.assertNotIn("<script", page)

        result = search_catalogue(config, '"divine kingship"')
        self.assertEqual(len(result.hits), 1)
        self.assertEqual(result.hits[0].collection_id, "alpha")
        self.assertEqual(result.hits[0].path.resolve(), (alpha / "works/work-1.html").resolve())
        self.assertEqual(result.hits[0].fragment, "representation-main")
        self.assertEqual(search_catalogue(config, "navigation").hits, ())
        self.assertEqual(search_catalogue(config, "citation").hits, ())
        self.assertEqual(search_catalogue(config, "script").hits, ())

    def test_filters_and_cli_use_current_catalogue(self) -> None:
        self.create_reader(
            "alpha",
            body="s\u0301iva beside the river.",
            language="sux-Latn",
            kind="transliteration",
        )
        config = load_config(self.config_path)
        build_catalogue(config, ("alpha",))

        derive_output = StringIO()
        with redirect_stdout(derive_output):
            derive_exit = main(
                [
                    "derive",
                    "catalogue",
                    "alpha",
                    "-c",
                    str(self.config_path),
                    "--json",
                ]
            )
        self.assertEqual(derive_exit, 0)
        self.assertEqual(
            json.loads(derive_output.getvalue())["metadata"]["status"],
            "complete",
        )

        open_output = StringIO()
        with redirect_stdout(open_output):
            open_exit = main(
                [
                    "open",
                    "catalogue",
                    "-c",
                    str(self.config_path),
                    "--print-only",
                ]
            )
        self.assertEqual(open_exit, 0)
        self.assertTrue(Path(open_output.getvalue().strip()).is_file())

        self.assertEqual(
            len(search_catalogue(config, "river", languages=("sux-Latn",)).hits),
            1,
        )
        self.assertEqual(len(search_catalogue(config, "śiva").hits), 1)
        self.assertEqual(
            search_catalogue(config, "river", languages=("en",)).hits,
            (),
        )
        output = StringIO()
        with redirect_stdout(output):
            exit_code = main(
                [
                    "search",
                    "river",
                    "-c",
                    str(self.config_path),
                    "--json",
                ]
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue())["hits"][0]["collection_id"], "alpha")

    def test_no_usable_reader_is_incomplete_and_does_not_advance_pointer(self) -> None:
        result = build_catalogue(load_config(self.config_path))

        self.assertEqual(result.metadata["status"], "incomplete")
        self.assertFalse(result.current_catalogue_updated)
        self.assertIsNone(result.current_index_path)
        with self.assertRaisesRegex(AnalysisError, "no current"):
            current_catalogue_index(load_config(self.config_path))

    def test_rejects_unsafe_catalogue_pointer(self) -> None:
        derived = self.root / "data/derived"
        derived.mkdir(parents=True)
        (derived / "LATEST-CATALOGUE").write_text("../outside\n", encoding="utf-8")

        with self.assertRaisesRegex(AnalysisError, "unsafe catalogue pointer"):
            current_catalogue_index(load_config(self.config_path))

    def test_rejects_symlinked_catalogue_and_reader_path_components(self) -> None:
        self.create_reader("alpha")
        config = load_config(self.config_path)
        build_catalogue(config, ("alpha",))
        derived = self.root / "data/derived"
        generations = derived / "catalogue-generations"
        moved_generations = self.root / "moved-catalogues"
        generations.rename(moved_generations)
        generations.symlink_to(moved_generations, target_is_directory=True)
        with self.assertRaisesRegex(AnalysisError, "generations directory is unsafe"):
            current_catalogue_index(config)
        generations.unlink()
        moved_generations.rename(generations)

        alpha = derived / "collections/alpha"
        moved_alpha = derived / "collections/alpha-real"
        alpha.rename(moved_alpha)
        alpha.symlink_to(moved_alpha, target_is_directory=True)
        with self.assertRaisesRegex(AnalysisError, "reader root is unsafe"):
            search_catalogue(config, "divine")

    def test_rejects_symlinked_canonical_reader_ancestor(self) -> None:
        self.create_reader("alpha")
        derived = self.root / "data/derived"
        collections = derived / "collections"
        moved_collections = derived / "collections-real"
        collections.rename(moved_collections)
        collections.symlink_to(moved_collections, target_is_directory=True)

        with self.assertRaisesRegex(AnalysisError, "directory component is unsafe"):
            build_catalogue(load_config(self.config_path), ("alpha",))

    def test_extraction_stops_at_representation_end_with_omitted_child_end_tag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "work.html").write_text(
                '<article id="representation-main"><p>inside</article><footer>outside</footer>',
                encoding="utf-8",
            )
            representation = AccessRepresentation(
                "tp:alpha/representation/main",
                "Main",
                "text",
                "en",
                "work.html#representation-main",
                (),
            )

            self.assertEqual(_extract_representation(root, representation), "inside")

    def test_extraction_tracks_excluded_tags_with_optional_and_void_end_tags(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "work.html").write_text(
                '<article id="representation-main">before'
                '<aside class="reader-citation"><p>noise<br/></aside>'
                "after</article>",
                encoding="utf-8",
            )
            representation = AccessRepresentation(
                "tp:alpha/representation/main",
                "Main",
                "text",
                "en",
                "work.html#representation-main",
                (),
            )

            self.assertEqual(_extract_representation(root, representation), "before after")

    def test_builds_and_searches_801_document_index(self) -> None:
        self.create_reader("alpha", count=801)

        result = build_catalogue(load_config(self.config_path), ("alpha",))

        self.assertEqual(result.metadata["summary"]["document_count"], 801)
        hits = search_catalogue(load_config(self.config_path), "needle801").hits
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].title, "Work 801")

    def test_database_validation_checks_external_content_postings(self) -> None:
        self.create_reader("alpha")
        result = build_catalogue(load_config(self.config_path), ("alpha",))
        result.database_path.chmod(0o644)
        result.output_directory.chmod(0o755)
        with closing(sqlite3.connect(result.database_path)) as connection:
            connection.execute(
                "INSERT INTO document_fts(document_fts) VALUES ('delete-all')"
            )
            connection.commit()

        with self.assertRaises(sqlite3.DatabaseError):
            _validate_database(result.database_path, verify_external_content=True)

    def test_catalogue_ingests_canonical_schema_one_reader(self) -> None:
        self.create_reader("alpha", schema_one=True)

        result = build_catalogue(load_config(self.config_path), ("alpha",))

        self.assertEqual(result.metadata["summary"]["document_count"], 1)
        self.assertEqual(search_catalogue(load_config(self.config_path), "divine").hits[0].title, "Work One")

    def test_database_validation_accepts_persisted_schema_one_index(self) -> None:
        self.create_reader("alpha")
        result = build_catalogue(load_config(self.config_path), ("alpha",))
        result.output_directory.chmod(0o755)
        result.database_path.chmod(0o644)
        with closing(sqlite3.connect(result.database_path)) as connection:
            connection.execute("PRAGMA user_version=1")

        _validate_database(result.database_path)
        with self.assertRaisesRegex(ValueError, "schema version"):
            _validate_database(result.database_path, expected_schema_version=2)

    def test_compact_facets_do_not_infer_hierarchy_from_value_punctuation(self) -> None:
        rendered = _render_item_facets(
            [
                {"label": "Category", "values": ["Narrative"]},
                {"label": "Path", "values": ["Narrative › Heroes › Gilgameš"]},
            ]
        )

        self.assertIn("<dt>Category</dt>", rendered)
        self.assertIn("<dt>Path</dt>", rendered)
        self.assertEqual(rendered.count("Narrative"), 2)


if __name__ == "__main__":
    unittest.main()
