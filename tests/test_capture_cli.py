from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest

from text_preserver.cli import main

from tests.test_config import VALID_CONFIG


class CaptureCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.config_path = self.root / "collections.toml"
        self.config_path.write_text(VALID_CONFIG, encoding="utf-8")

    def test_json_dry_run_has_no_filesystem_side_effects(self) -> None:
        output = StringIO()

        with redirect_stdout(output):
            exit_code = main(
                [
                    "capture",
                    "test-collection",
                    "--config",
                    str(self.config_path),
                    "--dry-run",
                    "--json",
                ]
            )

        document = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(document["collection_id"], "test-collection")
        self.assertEqual(len(document["commands"]), 2)
        self.assertFalse((self.root / "data").exists())

    def test_source_filter_is_repeatable_and_preserves_recipe_order(self) -> None:
        output = StringIO()

        with redirect_stdout(output):
            exit_code = main(
                [
                    "capture",
                    "test-collection",
                    "-c",
                    str(self.config_path),
                    "--source",
                    "dataset",
                    "--dry-run",
                    "--json",
                ]
            )

        document = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual([item["source_id"] for item in document["commands"]], ["dataset"])

    def test_execution_remains_disabled(self) -> None:
        errors = StringIO()

        with redirect_stderr(errors):
            exit_code = main(
                ["capture", "test-collection", "-c", str(self.config_path)]
            )

        self.assertEqual(exit_code, 2)
        self.assertIn("execution is not implemented", errors.getvalue())
        self.assertFalse((self.root / "data").exists())

    def test_unknown_collection_returns_usage_error(self) -> None:
        errors = StringIO()

        with redirect_stderr(errors):
            exit_code = main(
                [
                    "capture",
                    "missing",
                    "-c",
                    str(self.config_path),
                    "--dry-run",
                ]
            )

        self.assertEqual(exit_code, 2)
        self.assertIn("unknown collection", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
