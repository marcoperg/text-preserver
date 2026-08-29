from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest

from text_preserver.cli import main

from tests.test_bagit import finalized_capture
from tests.test_wacz import finalized_warc_capture


class ExportCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)

    def test_bagit_export_and_independent_validation(self) -> None:
        capture = finalized_capture(self.root)
        output = self.root / "capture.bag"
        stdout = StringIO()

        with redirect_stdout(stdout):
            exit_code = main(
                ["export", "bagit", str(capture), str(output), "--profile", "private", "--json"]
            )

        result = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(Path(result["path"]), output.resolve())
        self.assertEqual(result["profile"], "private")

        stdout = StringIO()
        with redirect_stdout(stdout):
            exit_code = main(["export", "validate-bagit", str(output), "--json"])
        validation = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(validation["ok"])

    def test_wacz_export_selects_preservation_warcs_and_validates(self) -> None:
        capture, _warc_path, _source_bytes = finalized_warc_capture(self.root)
        output = self.root / "capture.wacz"
        stdout = StringIO()

        with redirect_stdout(stdout):
            exit_code = main(
                [
                    "export",
                    "wacz",
                    str(capture),
                    str(output),
                    "--profile",
                    "public",
                    "--main-page-url",
                    "https://example.test/hello",
                    "--json",
                ]
            )

        result = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["warc_files"], 1)
        self.assertEqual(result["indexed_records"], 2)

        stdout = StringIO()
        with redirect_stdout(stdout):
            exit_code = main(["export", "validate-wacz", str(output), "--json"])
        validation = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(validation["ok"])


if __name__ == "__main__":
    unittest.main()
