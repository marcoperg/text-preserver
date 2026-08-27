from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import patch

from text_preserver.capture import CaptureExecutionError, execute_capture
from text_preserver.capture.execute import collection_lock, termination_signals_as_interrupts
from text_preserver.config import load_config

from tests.test_config import VALID_CONFIG


class CaptureExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.config_path = self.root / "collections.toml"
        self.config_path.write_text(VALID_CONFIG, encoding="utf-8")

    def test_collection_lock_rejects_overlapping_capture(self) -> None:
        config = load_config(self.config_path)

        with collection_lock(config.project.archive_root, "test-collection"):
            with self.assertRaisesRegex(CaptureExecutionError, "another capture is active"):
                execute_capture(
                    config,
                    "test-collection",
                    source_ids=["web"],
                    capture_id="20260827T120000Z-a1b2c3",
                )

        self.assertFalse(
            (
                config.project.archive_root
                / "collections/test-collection/captures/20260827T120000Z-a1b2c3"
            ).exists()
        )

    def test_disabled_collection_cannot_execute(self) -> None:
        repository_root = Path(__file__).parents[1]
        config = load_config(repository_root / "collections.example.toml")

        with self.assertRaisesRegex(CaptureExecutionError, "is disabled"):
            execute_capture(config, "example-corpus", source_ids=["web"])

        self.assertFalse(config.project.archive_root.exists())

    def test_termination_signal_uses_interruption_path(self) -> None:
        with self.assertRaises(KeyboardInterrupt):
            with termination_signals_as_interrupts():
                os.kill(os.getpid(), signal.SIGTERM)

    def test_failed_process_is_preserved_in_status_records(self) -> None:
        invalid = VALID_CONFIG.replace(
            'seeds = ["https://example.org/index.html"]',
            'seeds = ["http://127.0.0.1:1/unavailable"]',
        ).replace(
            'allowed_hosts = ["example.org"]',
            'allowed_hosts = ["127.0.0.1"]',
            1,
        ).replace(
            'wait = 2.0\nquota = "50M"',
            'wait = 0\nquota = "50M"\ntimeout = 1\ntries = 1\nrandom_wait = false',
        )
        self.config_path.write_text(invalid, encoding="utf-8")
        config = load_config(self.config_path)

        result = execute_capture(
            config,
            "test-collection",
            source_ids=["web"],
            capture_id="20260827T120000Z-a1b2c3",
            operator_note="Expected local failure",
        )

        self.assertEqual(result.status, "failed")
        capture = json.loads(
            (result.capture_directory / "capture.json").read_text(encoding="utf-8")
        )
        source = json.loads(
            (
                result.capture_directory
                / "sources/web/metadata/result.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(capture["status"], "failed")
        self.assertEqual(capture["operator_note"], "Expected local failure")
        self.assertEqual(source["status"], "failed")
        self.assertIsNotNone(source["exit_code"])
        self.assertTrue((result.capture_directory / "metadata/input-config.toml").is_file())
        self.assertFalse((result.capture_directory.parent.parent / "LATEST").exists())
        self.assertEqual(
            (result.capture_directory / "metadata/input-config.toml").read_bytes(),
            config.input_bytes,
        )

    @patch("text_preserver.capture.execute._run_wget", side_effect=KeyboardInterrupt)
    def test_interruption_is_preserved_at_source_and_capture_level(
        self,
        _mock_run: object,
    ) -> None:
        config = load_config(self.config_path)
        capture_id = "20260827T120000Z-a1b2c3"

        with self.assertRaises(KeyboardInterrupt):
            execute_capture(
                config,
                "test-collection",
                source_ids=["web"],
                capture_id=capture_id,
            )

        capture_directory = (
            config.project.archive_root
            / "collections"
            / "test-collection"
            / "captures"
            / capture_id
        )
        capture = json.loads(
            (capture_directory / "capture.json").read_text(encoding="utf-8")
        )
        source = json.loads(
            (capture_directory / "sources/web/metadata/result.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(capture["status"], "interrupted")
        self.assertEqual(capture["sources"][0]["status"], "interrupted")
        self.assertEqual(source["status"], "interrupted")
        self.assertFalse((capture_directory.parent.parent / "LATEST").exists())


if __name__ == "__main__":
    unittest.main()
