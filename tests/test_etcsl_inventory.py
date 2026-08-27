from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


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


if __name__ == "__main__":
    unittest.main()
