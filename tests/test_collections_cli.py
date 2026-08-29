from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest

from text_preserver.cli import main

from tests.test_config import VALID_CONFIG


class CollectionsCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.config_path = Path(self.temporary_directory.name) / "collections.toml"
        self.config_path.write_text(VALID_CONFIG, encoding="utf-8")

    def test_lists_collections_as_json(self) -> None:
        output = StringIO()

        with redirect_stdout(output):
            exit_code = main(["collections", "list", "-c", str(self.config_path), "--json"])

        document = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(document["collections"][0]["id"], "test-collection")
        self.assertEqual(document["collections"][0]["source_count"], 2)

    def test_shows_resolved_collection(self) -> None:
        output = StringIO()

        with redirect_stdout(output):
            exit_code = main(
                ["collections", "show", "test-collection", "-c", str(self.config_path), "--json"]
            )

        document = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual([source["id"] for source in document["sources"]], ["web", "dataset"])
        self.assertEqual(document["sources"][0]["capture"]["quota"], "50M")

    def test_unknown_collection_is_an_error(self) -> None:
        errors = StringIO()

        with redirect_stderr(errors):
            exit_code = main(
                ["collections", "show", "missing", "-c", str(self.config_path)]
            )

        self.assertEqual(exit_code, 2)
        self.assertIn("unknown collection", errors.getvalue())

    def test_status_reports_four_dimensions_without_aggregate(self) -> None:
        output = StringIO()

        with redirect_stdout(output):
            exit_code = main(
                ["collections", "status", "test-collection", "-c", str(self.config_path), "--json"]
            )

        document = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(
            {document[name]["state"] for name in ("acquisition", "fixity", "validation", "access")},
            {"not_acquired", "not_finalized", "not_run"},
        )
        self.assertNotIn("status", document)

    def test_status_text_has_four_separate_lines(self) -> None:
        output = StringIO()

        with redirect_stdout(output):
            exit_code = main(
                ["collections", "status", "test-collection", "-c", str(self.config_path)]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(output.getvalue().splitlines()), 4)


if __name__ == "__main__":
    unittest.main()
