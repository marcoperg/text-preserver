from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from text_preserver.analysis import (
    AnalysisError,
    analyze_preservation,
    build_static_reader,
    current_reader_index,
)
from text_preserver.cli import main
from text_preserver.config import load_config
from text_preserver.manifest import finalize_capture, verify_capture


REPOSITORY_ROOT = Path(__file__).parents[1]
ETCSL_RECIPE = REPOSITORY_ROOT / "collections/etcsl"


class PreservationAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        recipe_root = self.root / "recipe"
        recipe_root.mkdir()
        shutil.copyfile(ETCSL_RECIPE / "inventory.py", recipe_root / "inventory.py")
        (recipe_root / "collection.toml").write_text(
            """
[collection]
id = "etcsl-fixture"
title = "ETCSL Fixture"

[collection.analysis]
inventory_adapter = "inventory.py"
reader_source = "ota-dataset"
expected_work_count = 4
required_representation_kinds = ["transliteration"]

[[collection.sources]]
id = "ota-record"
kind = "web"
title = "Repository record"
seeds = ["https://example.org/record"]
allowed_hosts = ["example.org"]

[[collection.sources]]
id = "ota-dataset"
kind = "http-file"
title = "Dataset"
seeds = ["https://example.org/etcsl.zip"]
allowed_hosts = ["example.org"]

[collection.sources.capture]
recursive = false
page_requisites = false
convert_links = false
adjust_extension = false
""".strip(),
            encoding="utf-8",
        )
        self.config_path = self.root / "collections.toml"
        self.config_path.write_text(
            """
recipes = ["recipe/collection.toml"]

[project]
archive_root = "./data/archive"
derived_root = "./data/derived"
workspace_root = "./data/workspace"
operator = "Test operator"
contact = "mailto:test@example.org"
user_agent = "text-preserver-test/1.0"
""".strip(),
            encoding="utf-8",
        )
        self.capture_id = "20260827T120000Z-a1b2c3"
        self.capture = (
            self.root
            / "data/archive/collections/etcsl-fixture/captures"
            / self.capture_id
        )

    def create_capture(
        self,
        *,
        omit_translation: bool = False,
        metadata_capture_id: str | None = None,
        adapter_source: str | None = None,
    ) -> None:
        record = self.capture / "sources/ota-record/mirror"
        dataset = self.capture / "sources/ota-dataset/mirror"
        record.mkdir(parents=True)
        dataset.mkdir(parents=True)
        recipe_assets = self.capture / "metadata/recipe-assets"
        recipe_assets.mkdir(parents=True)
        if adapter_source is None:
            shutil.copyfile(ETCSL_RECIPE / "inventory.py", recipe_assets / "inventory.py")
        else:
            (recipe_assets / "inventory.py").write_text(adapter_source, encoding="utf-8")
        shutil.copyfile(
            ETCSL_RECIPE / "fixtures/catalogue.html",
            dataset / "etcslfullcat.html",
        )
        for name in (
            "contents.txt",
            "corphdr.xml",
            "etcsl-extensions.dtd",
            "etcsl-extensions.ent",
            "etcsl-sux.ent",
            "etcsl.xml",
            "etcslmanual.html",
            "header2518.xml",
            "readme.txt",
        ):
            (dataset / name).write_text("", encoding="utf-8")
        ids = ["0.1.1", "1.8.1.5.1", "2.4.1.a", "4.03.1"]
        with zipfile.ZipFile(dataset / "etcsl.zip", "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("etcsl/tei/tei2.dtd", "")
            for identifier in ids:
                archive.writestr(
                    f"etcsl/transliterations/c.{identifier}.xml",
                    f'<TEI.2 id="c.{identifier}"><text /></TEI.2>',
                )
            for identifier in ids[1:]:
                if omit_translation and identifier == "2.4.1.a":
                    continue
                archive.writestr(
                    f"etcsl/translations/t.{identifier}.xml",
                    f'<TEI.2 id="t.{identifier}"><text /></TEI.2>',
                )
        (self.capture / "capture.json").write_text(
            json.dumps(
                {
                    "capture_id": metadata_capture_id or self.capture_id,
                    "collection_id": "etcsl-fixture",
                    "status": "complete",
                    "sources": [
                        {"source_id": "ota-record", "status": "complete"},
                        {"source_id": "ota-dataset", "status": "complete"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        finalize_capture(self.capture)

    def test_writes_complete_report_outside_verified_capture(self) -> None:
        self.create_capture()
        config = load_config(self.config_path)

        result = analyze_preservation(config, "etcsl-fixture", self.capture)

        self.assertEqual(result.status, "complete")
        self.assertTrue(result.report_path.is_file())
        self.assertTrue(verify_capture(self.capture).ok)
        self.assertEqual(result.report["deposit"]["transliteration_count"], 4)
        self.assertEqual(result.report["mapping"]["missing_deposit_translations"], [])
        self.assertEqual(result.report["collection_id"], "etcsl-fixture")
        self.assertEqual(result.report["capture_id"], self.capture_id)
        self.assertEqual(result.report["analyzer"]["source"], "preserved_capture")

    def test_rejects_unsafe_capture_id_before_writing_report(self) -> None:
        self.create_capture(metadata_capture_id="../escape")

        with self.assertRaisesRegex(AnalysisError, "unsafe capture ID"):
            analyze_preservation(load_config(self.config_path), "etcsl-fixture", self.capture)

        self.assertFalse((self.root / "data/derived/collections/escape").exists())

    def test_rejects_capture_id_that_does_not_match_directory(self) -> None:
        other_id = "20260827T120001Z-b1c2d3"
        self.create_capture(metadata_capture_id=other_id)

        with self.assertRaisesRegex(AnalysisError, "does not match directory"):
            analyze_preservation(load_config(self.config_path), "etcsl-fixture", self.capture)

        self.assertFalse(
            (self.root / f"data/derived/collections/etcsl-fixture/captures/{other_id}").exists()
        )

    def test_detects_adapter_mutation_of_capture(self) -> None:
        self.create_capture(
            adapter_source="""
def analyze_capture(capture_directory, **kwargs):
    (capture_directory / "capture.json").write_text("{}", encoding="utf-8")
    return {"status": "complete", "errors": [], "warnings": []}
""".strip()
        )

        with self.assertRaisesRegex(AnalysisError, "capture changed during analysis"):
            analyze_preservation(load_config(self.config_path), "etcsl-fixture", self.capture)

        self.assertFalse(
            (
                self.root
                / f"data/derived/collections/etcsl-fixture/captures/{self.capture_id}/completeness.json"
            ).exists()
        )

    def test_rejects_symlink_in_derived_output_path(self) -> None:
        self.create_capture()
        derived_root = self.root / "data/derived"
        outside = self.root / "outside"
        derived_root.mkdir(parents=True)
        outside.mkdir()
        (derived_root / "collections").symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(AnalysisError, "must not contain symlinks"):
            analyze_preservation(load_config(self.config_path), "etcsl-fixture", self.capture)

        self.assertEqual(list(outside.iterdir()), [])

    def test_missing_deposit_translation_is_incomplete(self) -> None:
        self.create_capture(omit_translation=True)
        config = load_config(self.config_path)

        result = analyze_preservation(config, "etcsl-fixture", self.capture)

        self.assertEqual(result.status, "incomplete")
        self.assertEqual(
            result.report["mapping"]["missing_deposit_translations"],
            ["2.4.1.a"],
        )

    def test_builds_static_reader_without_changing_capture(self) -> None:
        self.create_capture()

        result = build_static_reader(
            load_config(self.config_path),
            "etcsl-fixture",
            self.capture,
        )

        self.assertTrue(result.index_path.is_file())
        self.assertTrue(result.output_directory.is_symlink())
        self.assertTrue(result.current_reader_updated)
        self.assertIsNotNone(result.current_index_path)
        current_reader = self.root / "data/derived/collections/etcsl-fixture/reader"
        self.assertTrue(current_reader.is_symlink())
        self.assertIn("reader-generations", current_reader.readlink().parts)
        self.assertEqual(result.index_path.stat().st_mode & 0o222, 0)
        self.assertEqual(
            current_reader_index(load_config(self.config_path), "etcsl-fixture").resolve(),
            result.index_path.resolve(),
        )
        self.assertTrue(result.metadata_path.is_file())
        self.assertEqual(result.metadata["summary"]["work_count"], 4)
        self.assertEqual(result.metadata["renderer"]["source"], "current_recipe")
        self.assertTrue((result.output_directory / "works/1.8.1.5.1.html").is_file())
        self.assertIn("Composition 1.8.1.5.1", result.index_path.read_text(encoding="utf-8"))
        self.assertTrue(verify_capture(self.capture).ok)

        first_generation = result.output_directory.resolve()
        rebuilt = build_static_reader(
            load_config(self.config_path),
            "etcsl-fixture",
            self.capture,
        )
        self.assertNotEqual(rebuilt.output_directory.resolve(), first_generation)
        self.assertTrue(rebuilt.index_path.is_file())
        self.assertEqual(
            current_reader_index(load_config(self.config_path), "etcsl-fixture").resolve(),
            rebuilt.index_path.resolve(),
        )

    def test_builds_streaming_static_reader(self) -> None:
        self.create_capture()
        (self.root / "recipe/inventory.py").write_text(
            """
def write_static_reader(capture_directory, *, output_directory, expected_work_count):
    works = output_directory / "works"
    works.mkdir()
    (output_directory / "index.html").write_text("<h1>Index</h1>", encoding="utf-8")
    (works / "one.html").write_text("<p>Complete text</p>", encoding="utf-8")
    return {
        "status": "complete",
        "summary": {"work_count": expected_work_count},
        "warnings": [],
    }
""".strip(),
            encoding="utf-8",
        )

        result = build_static_reader(
            load_config(self.config_path),
            "etcsl-fixture",
            self.capture,
        )

        self.assertEqual(result.metadata["summary"]["output_file_count"], 2)
        self.assertGreater(result.metadata["summary"]["output_bytes"], 0)
        self.assertTrue((result.output_directory / "works/one.html").is_file())
        self.assertEqual(result.index_path.stat().st_mode & 0o222, 0)
        self.assertTrue(verify_capture(self.capture).ok)

    def test_streaming_reader_rejects_reserved_metadata(self) -> None:
        self.create_capture()
        (self.root / "recipe/inventory.py").write_text(
            """
def write_static_reader(capture_directory, *, output_directory, expected_work_count):
    (output_directory / "index.html").write_text("ok", encoding="utf-8")
    (output_directory / "metadata.json").write_text("{}", encoding="utf-8")
    return {"status": "complete", "summary": {}, "warnings": []}
""".strip(),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(AnalysisError, "reserved metadata.json"):
            build_static_reader(
                load_config(self.config_path),
                "etcsl-fixture",
                self.capture,
            )

        generations = (
            self.root
            / f"data/derived/collections/etcsl-fixture/captures/{self.capture_id}/reader-generations"
        )
        self.assertEqual(list(generations.iterdir()), [])

    def test_streaming_reader_rejects_hard_link_to_capture(self) -> None:
        self.create_capture()
        capture_mode = (self.capture / "capture.json").stat().st_mode
        (self.root / "recipe/inventory.py").write_text(
            """
import os

def write_static_reader(capture_directory, *, output_directory, expected_work_count):
    (output_directory / "index.html").write_text("ok", encoding="utf-8")
    os.link(capture_directory / "capture.json", output_directory / "capture.json")
    return {"status": "complete", "summary": {}, "warnings": []}
""".strip(),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(AnalysisError, "hard link"):
            build_static_reader(
                load_config(self.config_path),
                "etcsl-fixture",
                self.capture,
            )

        self.assertEqual((self.capture / "capture.json").stat().st_mode, capture_mode)
        self.assertEqual((self.capture / "capture.json").stat().st_nlink, 1)

    def test_reader_defaults_to_configured_source_pointer(self) -> None:
        self.create_capture()
        collection_root = self.capture.parent.parent
        (collection_root / "LATEST-ota-dataset").write_text(
            f"captures/{self.capture_id}\n",
            encoding="utf-8",
        )

        result = build_static_reader(load_config(self.config_path), "etcsl-fixture")

        self.assertEqual(result.capture_directory, self.capture.resolve())
        self.assertTrue(result.current_reader_updated)

    def test_reader_rejects_dangling_configured_source_pointer(self) -> None:
        self.create_capture()
        collection_root = self.capture.parent.parent
        (collection_root / "LATEST").write_text(
            f"captures/{self.capture_id}\n",
            encoding="utf-8",
        )
        (collection_root / "LATEST-ota-dataset").symlink_to(
            "captures/20260827T000000Z-dead00"
        )

        with self.assertRaisesRegex(AnalysisError, "capture pointer is unavailable"):
            build_static_reader(load_config(self.config_path), "etcsl-fixture")

    def test_reader_rejects_unsafe_adapter_output_path(self) -> None:
        self.create_capture()
        (self.root / "recipe/inventory.py").write_text(
            """
def render_static_reader(capture_directory, **kwargs):
    return {"status": "complete", "files": {"index.html": "ok", "../escape": "bad"}}
""".strip(),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(AnalysisError, "unsafe path"):
            build_static_reader(
                load_config(self.config_path),
                "etcsl-fixture",
                self.capture,
            )

        self.assertFalse((self.root / "data/derived/escape").exists())

    def test_reader_detects_adapter_mutation_of_capture(self) -> None:
        self.create_capture()
        (self.root / "recipe/inventory.py").write_text(
            """
def render_static_reader(capture_directory, **kwargs):
    (capture_directory / "capture.json").write_text("{}", encoding="utf-8")
    return {"status": "complete", "files": {"index.html": "ok"}}
""".strip(),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(AnalysisError, "capture changed during reader generation"):
            build_static_reader(
                load_config(self.config_path),
                "etcsl-fixture",
                self.capture,
            )

    def test_partial_reader_is_incomplete(self) -> None:
        self.create_capture(omit_translation=True)

        result = build_static_reader(
            load_config(self.config_path),
            "etcsl-fixture",
            self.capture,
        )

        self.assertEqual(result.metadata["status"], "incomplete")
        self.assertFalse(result.current_reader_updated)
        self.assertIsNone(result.current_index_path)

    def test_incomplete_reader_does_not_replace_current_reader(self) -> None:
        self.create_capture()
        config = load_config(self.config_path)
        complete = build_static_reader(config, "etcsl-fixture", self.capture)
        previous_index = complete.index_path.resolve()
        (self.root / "recipe/inventory.py").write_text(
            """
def render_static_reader(capture_directory, **kwargs):
    return {
        "status": "incomplete",
        "files": {"index.html": "incomplete"},
        "summary": {},
        "warnings": [],
    }
""".strip(),
            encoding="utf-8",
        )

        incomplete = build_static_reader(config, "etcsl-fixture", self.capture)

        self.assertFalse(incomplete.current_reader_updated)
        self.assertEqual(
            current_reader_index(config, "etcsl-fixture").resolve(),
            previous_index,
        )

    def test_reader_rejects_current_pointer_outside_collection_captures(self) -> None:
        self.create_capture()
        collection_root = self.root / "data/derived/collections/etcsl-fixture"
        outside = self.root / "outside-reader"
        collection_root.mkdir(parents=True)
        outside.mkdir()
        (collection_root / "reader").symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(AnalysisError, "escapes collection captures"):
            build_static_reader(
                load_config(self.config_path),
                "etcsl-fixture",
                self.capture,
            )

    def test_reader_cli_emits_json(self) -> None:
        self.create_capture()
        output = StringIO()

        with redirect_stdout(output):
            exit_code = main(
                [
                    "derive",
                    "reader",
                    "etcsl-fixture",
                    str(self.capture),
                    "-c",
                    str(self.config_path),
                    "--json",
                ]
            )

        value = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(value["metadata"]["summary"]["work_count"], 4)
        self.assertTrue(Path(value["index_path"]).is_file())
        self.assertTrue(value["current_reader_updated"])

    def test_open_reader_cli_prints_stable_index(self) -> None:
        self.create_capture()
        build_static_reader(load_config(self.config_path), "etcsl-fixture", self.capture)
        output = StringIO()

        with redirect_stdout(output):
            exit_code = main(
                [
                    "open",
                    "reader",
                    "etcsl-fixture",
                    "-c",
                    str(self.config_path),
                    "--print-only",
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            Path(output.getvalue().strip()).resolve(),
            (
                self.root / "data/derived/collections/etcsl-fixture/reader/index.html"
            ).resolve(),
        )

    @patch("text_preserver.cli.webbrowser.open", return_value=True)
    def test_open_reader_cli_json_still_launches_browser(self, mock_open: object) -> None:
        self.create_capture()
        build_static_reader(load_config(self.config_path), "etcsl-fixture", self.capture)
        output = StringIO()

        with redirect_stdout(output):
            exit_code = main(
                [
                    "open",
                    "reader",
                    "etcsl-fixture",
                    "-c",
                    str(self.config_path),
                    "--json",
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue())["collection_id"], "etcsl-fixture")
        self.assertEqual(mock_open.call_count, 1)  # type: ignore[attr-defined]

    def test_cli_emits_json_and_uses_nonzero_for_incomplete_report(self) -> None:
        self.create_capture(omit_translation=True)
        output = StringIO()

        with redirect_stdout(output):
            exit_code = main(
                [
                    "analyze",
                    "preservation",
                    "etcsl-fixture",
                    str(self.capture),
                    "-c",
                    str(self.config_path),
                    "--json",
                ]
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(json.loads(output.getvalue())["report"]["status"], "incomplete")

    def test_rejects_capture_with_failed_fixity(self) -> None:
        self.create_capture()
        (self.capture / "capture.json").write_text("{}", encoding="utf-8")
        errors = StringIO()

        with redirect_stderr(errors):
            exit_code = main(
                [
                    "analyze",
                    "preservation",
                    "etcsl-fixture",
                    str(self.capture),
                    "-c",
                    str(self.config_path),
                ]
            )

        self.assertEqual(exit_code, 2)
        self.assertIn("fixity verification failed", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
