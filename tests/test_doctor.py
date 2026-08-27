from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from text_preserver.cli import main
from text_preserver.doctor import DoctorCheck, inspect_environment

from tests.test_config import VALID_CONFIG


class DoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)

    def write_config(self, contents: str = VALID_CONFIG) -> Path:
        path = self.root / "collections.toml"
        path.write_text(contents, encoding="utf-8")
        return path

    @patch(
        "text_preserver.doctor._inspect_wget",
        return_value=[DoctorCheck("GNU Wget", True, "test Wget")],
    )
    def test_valid_environment_reports_all_checks(self, _mock_wget: object) -> None:
        checks = inspect_environment(self.write_config())

        self.assertTrue(all(check.ok for check in checks))
        self.assertEqual(checks[1].name, "configuration")
        self.assertIn("1 collection(s)", checks[1].detail)
        self.assertIn("archive root", {check.name for check in checks})

    def test_invalid_configuration_stops_dependency_checks(self) -> None:
        checks = inspect_environment(self.root / "missing.toml")

        self.assertEqual(len(checks), 2)
        self.assertFalse(checks[-1].ok)
        self.assertEqual(checks[-1].name, "configuration")

    def test_cli_json_returns_failure_for_missing_configuration(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            exit_code = main(["doctor", "-c", str(self.root / "missing.toml"), "--json"])

        self.assertEqual(exit_code, 1)
        self.assertIn('"ok": false', output.getvalue())

    @patch(
        "text_preserver.doctor._inspect_wget",
        return_value=[DoctorCheck("GNU Wget", True, "test Wget")],
    )
    def test_rejects_regular_file_in_root_path(self, _mock_wget: object) -> None:
        blocker = self.root / "not-a-directory"
        blocker.write_text("content", encoding="utf-8")
        invalid_root = VALID_CONFIG.replace(
            'archive_root = "./data/archive"',
            'archive_root = "./not-a-directory/archive"',
        )

        checks = inspect_environment(self.write_config(invalid_root))

        archive_check = next(check for check in checks if check.name == "archive root")
        self.assertFalse(archive_check.ok)
        self.assertIn("not a directory", archive_check.detail)

    @patch(
        "text_preserver.doctor._inspect_wget",
        return_value=[DoctorCheck("GNU Wget", True, "test Wget")],
    )
    def test_reports_stale_running_capture(self, _mock_wget: object) -> None:
        config_path = self.write_config()
        capture = self.root / "data/archive/collections/test-collection/captures/stale"
        capture.mkdir(parents=True)
        (capture / "capture.json").write_text(
            '{"status":"running"}\n',
            encoding="utf-8",
        )

        checks = inspect_environment(config_path)

        stale = next(check for check in checks if check.name == "stale captures")
        self.assertFalse(stale.ok)
        self.assertIn("1 running capture", stale.detail)


if __name__ == "__main__":
    unittest.main()
