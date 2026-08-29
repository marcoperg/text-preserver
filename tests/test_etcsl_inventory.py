from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from text_preserver.adapters import _load_adapter


REPOSITORY_ROOT = Path(__file__).parents[1]
RECIPE_ROOT = REPOSITORY_ROOT / "collections/etcsl"
SPEC = importlib.util.spec_from_file_location("etcsl_inventory", RECIPE_ROOT / "inventory.py")
assert SPEC is not None and SPEC.loader is not None
inventory = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = inventory
SPEC.loader.exec_module(inventory)
reader, _READER_SOURCE = _load_adapter(RECIPE_ROOT / "reader.py")


class EtcslInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = (RECIPE_ROOT / "fixtures/catalogue.html").read_text(encoding="utf-8")
        self.expected_ids = (RECIPE_ROOT / "fixtures/expected-ids.txt").read_text(
            encoding="utf-8"
        ).splitlines()

    def test_extracts_variable_ids_titles_and_representations(self) -> None:
        compositions = inventory.extract_inventory(self.document)

        self.assertEqual([item.id for item in compositions], self.expected_ids)
        self.assertIsNone(compositions[0].translation_url)
        self.assertIn("(Version B)", compositions[1].title)
        self.assertIn("to Damgalnuna", compositions[3].title)

    def test_fixture_is_complete_against_expected_inventory(self) -> None:
        compositions = inventory.extract_inventory(self.document)

        report = inventory.build_report(
            compositions,
            expected_work_count=4,
            known_untranslated={"0.1.1"},
            expected_ids=self.expected_ids,
        )

        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["translation_count"], 3)
        self.assertEqual(report["errors"], [])

    def test_extracts_nested_deposited_catalogue_records(self) -> None:
        document = """
<ul><li>Category<ul>
<li><b>0.1.1</b> Untranslated record</li>
<li><b>1.8.1.1</b> Gilgame&#x0161; and <i>Aga</i></li>
<li>1.8.1.9 Unedited record</li>
</ul></li></ul>
""".strip()

        compositions = inventory.extract_inventory(document)

        self.assertEqual([item.id for item in compositions], ["0.1.1", "1.8.1.1"])
        self.assertEqual(compositions[1].title, "Gilgameš and Aga")
        self.assertIsNone(compositions[0].translation_url)
        self.assertEqual(
            compositions[1].transliteration_url,
            "etcsl/transliterations/c.1.8.1.1.xml",
        )

    def test_reports_missing_required_translation(self) -> None:
        document = self.document.replace(
            "<a href='etcsl.cgi?text=t.2.4.1.a'>translation</a>",
            "",
        )
        compositions = inventory.extract_inventory(document)

        report = inventory.build_report(
            compositions,
            expected_work_count=4,
            known_untranslated={"0.1.1"},
        )

        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(report["missing_translations"], ["2.4.1.a"])

    def test_rejects_mismatched_and_duplicate_ids(self) -> None:
        mismatched = self.document.replace("text=t.2.4.1.a", "text=t.2.4.1.b")
        with self.assertRaisesRegex(inventory.InventoryError, "different composition IDs"):
            inventory.extract_inventory(mismatched)

        duplicate = self.document.replace("</ul>", self.document.split("<li>", 1)[1] + "</ul>")
        with self.assertRaises(inventory.InventoryError):
            inventory.extract_inventory(duplicate)

    def test_public_recipe_loads(self) -> None:
        from text_preserver.config import load_config

        config_path = REPOSITORY_ROOT / "collections.example.toml"
        config = load_config(config_path)
        collection = next(item for item in config.collections if item.id == "etcsl")
        self.assertEqual(len(collection.sources), 2)
        dataset = next(item for item in collection.sources if item.id == "ota-dataset")
        self.assertEqual(len(dataset.seeds), 11)
        self.assertTrue(any("etcsl.zip?sequence=11" in seed for seed in dataset.seeds))
        self.assertFalse(any(seed.endswith("/allzip") for seed in dataset.seeds))
        self.assertEqual(collection.analysis["expected_work_count"], 394)
        self.assertEqual(collection.analysis["reader_adapter"], "reader.py")
        self.assertFalse(hasattr(inventory, "render_static_reader"))
        self.assertTrue(callable(reader.render_static_reader))

    def test_finds_complete_ota_package_with_query_suffixed_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for position, name in enumerate(sorted(inventory.OTA_DEPOSIT_FILENAMES), 1):
                (root / f"{name}?sequence={position}&isAllowed=y").write_bytes(b"fixture")

            files, errors = inventory._find_ota_package(root)

            self.assertEqual(set(files), inventory.OTA_DEPOSIT_FILENAMES)
            self.assertEqual(errors, [])

            files["readme.txt"].unlink()
            _files, errors = inventory._find_ota_package(root)
            self.assertIn("captured OTA deposit file was not found: readme.txt", errors)

    def test_support_graph_uses_sibling_ota_declarations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "etcsl.zip"
            extension = root / "etcsl-extensions.dtd"
            entities = root / "etcsl-sux.ent"
            extension.write_text(
                '<!ENTITY c "&#x0161;">\n<!ENTITY % sux SYSTEM "etcsl-sux.ent">\n%sux;',
                encoding="utf-8",
            )
            entities.write_text('<!ENTITY C "&#x0160;">', encoding="utf-8")
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr(
                    "etcsl/tei/tei2.dtd",
                    '<!ENTITY % ext SYSTEM "../etcsl-extensions.dtd">\n%ext;',
                )
            with zipfile.ZipFile(archive_path) as archive:
                visited, declared, missing = inventory._support_graph(
                    archive,
                    set(archive.namelist()),
                    supplemental_files={
                        "etcsl/etcsl-extensions.dtd": extension,
                        "etcsl/etcsl-sux.ent": entities,
                    },
                    roots=("etcsl/tei/tei2.dtd",),
                )

            self.assertEqual(missing, [])
            self.assertEqual(declared, {"C", "c"})
            self.assertIn("etcsl/etcsl-sux.ent", visited)

    def test_rejects_archive_above_entry_count_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "many-entries.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("first", b"")
                archive.writestr("second", b"")

            with patch.object(inventory, "MAX_ARCHIVE_ENTRIES", 1):
                with self.assertRaisesRegex(inventory.InventoryError, "entries.*safety limit"):
                    inventory._analyze_zip(path, 0)

    def test_rejects_xml_kind_in_wrong_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "swapped.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(
                    "etcsl/translations/c.1.1.1.xml",
                    '<TEI.2 id="c.1.1.1"><text /></TEI.2>',
                )

            with self.assertRaisesRegex(inventory.InventoryError, "does not match directory"):
                inventory._analyze_zip(path, 1)

    def test_reader_preserves_unknown_entities_and_escapes_markup(self) -> None:
        root, unresolved = reader._parse_reader_xml(
            """
<TEI.2 id="t.1.1.1"><teiHeader><fileDesc><titleStmt>
<title>&nosuch;&lt;script&gt;alert(1)&lt;/script&gt; -- an English prose translation</title>
</titleStmt></fileDesc></teiHeader><text><body><p n="1">Safe &nosuch; text.</p></body></text></TEI.2>
""".strip(),
            "fixture.xml",
        )

        title = reader._reader_title(root, "1.1.1")
        page = reader._render_work_page(
            "1.1.1",
            title,
            {"translation": root, "translation_path": "fixture.xml"},
            "20260827T120000Z-a1b2c3",
            "0" * 64,
            None,
            None,
        )

        self.assertEqual(unresolved, {"nosuch"})
        self.assertIn("&amp;nosuch;&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertNotIn("<script>", page)

    def test_reader_resolves_etcsl_entities_before_html_entities(self) -> None:
        root, unresolved = reader._parse_reader_xml(
            """
<TEI.2 id="t.1.1.1"><teiHeader><fileDesc><titleStmt>
<title>&C;ama&c;-&t;ab to Ilak-ni&aleph;id -- an English prose translation</title>
</titleStmt></fileDesc></teiHeader><text><body><p n="1">
&d;en-ki, &jic;ig, &mu;jen, A&s2;, &X;&hr;
</p></body></text></TEI.2>
""".strip(),
            "fixture.xml",
        )

        page = reader._render_work_page(
            "1.1.1",
            reader._reader_title(root, "1.1.1"),
            {"translation": root, "translation_path": "fixture.xml"},
            "20260827T120000Z-a1b2c3",
            "0" * 64,
            None,
            None,
        )

        self.assertEqual(unresolved, set())
        self.assertIn("Šamaš-ṭab to Ilak-ni’id", page)
        self.assertIn('class="determinative" title="ETCSL determinative d">d</sup>en-ki', page)
        self.assertIn('title="ETCSL determinative jic">ĝiš</sup>ig', page)
        self.assertIn('title="ETCSL determinative mu">mu</sup>jen', page)
        self.assertIn("A₂, …", page)
        self.assertIn('role="separator" title="horizontal ruling">―</span>', page)
        self.assertNotIn("&amp;aleph;", page)

    def test_reader_handles_translation_without_transliteration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            capture = Path(directory) / "20260827T120000Z-a1b2c3"
            mirror = capture / "sources/ota-dataset/mirror"
            mirror.mkdir(parents=True)
            with zipfile.ZipFile(mirror / "etcsl.zip", "w") as archive:
                archive.writestr("etcsl/tei/tei2.dtd", "")
                archive.writestr(
                    "etcsl/translations/t.1.1.1.xml",
                    """
<TEI.2 id="t.1.1.1"><teiHeader><fileDesc><titleStmt>
<title>Example -- an English prose translation</title>
</titleStmt></fileDesc></teiHeader><text><body><p n="1">Text.</p></body></text></TEI.2>
""".strip(),
                )

            payload = reader.render_static_reader(capture, expected_work_count=1)
            page = payload["files"]["works/1.1.1.html"]

            self.assertEqual(payload["status"], "incomplete")
            self.assertIn("translation", payload["files"]["index.html"])
            self.assertNotIn(">transliteration</span>", payload["files"]["index.html"])
            self.assertIn("No transliteration is present", page)

    def test_reader_is_incomplete_for_mismatched_root_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            capture = Path(directory) / "20260827T120000Z-a1b2c3"
            mirror = capture / "sources/ota-dataset/mirror"
            mirror.mkdir(parents=True)
            with zipfile.ZipFile(mirror / "etcsl.zip", "w") as archive:
                archive.writestr("etcsl/tei/tei2.dtd", "")
                archive.writestr(
                    "etcsl/transliterations/c.1.1.1.xml",
                    '<TEI.2 id="c.9.9.9"><text><body><l n="1">Text.</l></body></text></TEI.2>',
                )
                archive.writestr(
                    "etcsl/translations/t.1.1.1.xml",
                    '<TEI.2 id="t.1.1.1"><text><body><p n="1">Text.</p></body></text></TEI.2>',
                )

            payload = reader.render_static_reader(capture, expected_work_count=1)

            self.assertEqual(payload["status"], "incomplete")
            self.assertTrue(
                any("root name or ID" in warning for warning in payload["warnings"])
            )

    def test_reader_requires_translation_for_the_correct_composition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            capture = Path(directory) / "20260827T120000Z-a1b2c3"
            mirror = capture / "sources/ota-dataset/mirror"
            mirror.mkdir(parents=True)
            with zipfile.ZipFile(mirror / "etcsl.zip", "w") as archive:
                archive.writestr("etcsl/tei/tei2.dtd", "")
                for identifier in ("0.1.1", "1.1.1"):
                    archive.writestr(
                        f"etcsl/transliterations/c.{identifier}.xml",
                        f'<TEI.2 id="c.{identifier}"><text><body><l n="1">Text.</l></body></text></TEI.2>',
                    )
                archive.writestr(
                    "etcsl/translations/t.0.1.1.xml",
                    '<TEI.2 id="t.0.1.1"><text><body><p n="1">Text.</p></body></text></TEI.2>',
                )

            payload = reader.render_static_reader(capture, expected_work_count=2)

            self.assertEqual(payload["status"], "incomplete")


if __name__ == "__main__":
    unittest.main()
