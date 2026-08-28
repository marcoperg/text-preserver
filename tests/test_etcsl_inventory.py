from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile


REPOSITORY_ROOT = Path(__file__).parents[1]
RECIPE_ROOT = REPOSITORY_ROOT / "collections/etcsl"
SPEC = importlib.util.spec_from_file_location("etcsl_inventory", RECIPE_ROOT / "inventory.py")
assert SPEC is not None and SPEC.loader is not None
inventory = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = inventory
SPEC.loader.exec_module(inventory)


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
        self.assertEqual(len(collection.sources), 3)
        self.assertEqual(collection.analysis["expected_work_count"], 394)

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
        root, unresolved = inventory._parse_reader_xml(
            """
<TEI.2 id="t.1.1.1"><teiHeader><fileDesc><titleStmt>
<title>&h;&lt;script&gt;alert(1)&lt;/script&gt; -- an English prose translation</title>
</titleStmt></fileDesc></teiHeader><text><body><p n="1">Safe &h; text.</p></body></text></TEI.2>
""".strip(),
            "fixture.xml",
        )

        title = inventory._reader_title(root, "1.1.1")
        page = inventory._render_work_page(
            "1.1.1",
            title,
            {"translation": root, "translation_path": "fixture.xml"},
            "20260827T120000Z-a1b2c3",
            "0" * 64,
            None,
            None,
        )

        self.assertEqual(unresolved, {"h"})
        self.assertIn("&amp;h;&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertNotIn("<script>", page)

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

            payload = inventory.render_static_reader(capture, expected_work_count=1)
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

            payload = inventory.render_static_reader(capture, expected_work_count=1)

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

            payload = inventory.render_static_reader(capture, expected_work_count=2)

            self.assertEqual(payload["status"], "incomplete")


if __name__ == "__main__":
    unittest.main()
