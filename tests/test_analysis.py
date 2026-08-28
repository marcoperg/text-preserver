from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import shutil
import tempfile
import unittest
import zipfile

from text_preserver.analysis import AnalysisError, analyze_preservation
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
expected_work_count = 4
required_representation_kinds = ["transliteration"]

[[collection.sources]]
id = "historical-web"
kind = "web"
title = "Website"
seeds = ["https://example.org/catalogue"]
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
        web = self.capture / "sources/historical-web/mirror"
        dataset = self.capture / "sources/ota-dataset/mirror"
        web.mkdir(parents=True)
        dataset.mkdir(parents=True)
        recipe_assets = self.capture / "metadata/recipe-assets"
        recipe_assets.mkdir(parents=True)
        if adapter_source is None:
            shutil.copyfile(ETCSL_RECIPE / "inventory.py", recipe_assets / "inventory.py")
        else:
            (recipe_assets / "inventory.py").write_text(adapter_source, encoding="utf-8")
        shutil.copyfile(ETCSL_RECIPE / "fixtures/catalogue.html", web / "catalogue.html")
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
                        {"source_id": "historical-web", "status": "complete"},
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
