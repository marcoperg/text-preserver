from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from text_preserver.capture import CaptureExecutionError, execute_capture
from text_preserver.capture.execute import (
    _valid_pointer_target,
    collection_lock,
    termination_signals_as_interrupts,
)
from text_preserver.config import load_config
from text_preserver.manifest import finalize_capture

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
        self.config_path.write_text(
            VALID_CONFIG.replace("enabled = true", "enabled = false"),
            encoding="utf-8",
        )
        config = load_config(self.config_path)

        with self.assertRaisesRegex(CaptureExecutionError, "is disabled"):
            execute_capture(config, "test-collection", source_ids=["web"])

        self.assertFalse(config.project.archive_root.exists())

    def test_termination_signal_uses_interruption_path(self) -> None:
        with self.assertRaises(KeyboardInterrupt):
            with termination_signals_as_interrupts():
                os.kill(os.getpid(), signal.SIGTERM)

    def test_malformed_verified_capture_cannot_block_pointer_repair(self) -> None:
        capture = (
            self.root
            / "data/archive/collections/test-collection/captures/20260829T120000Z-c1d2e3"
        )
        metadata = capture / "metadata"
        metadata.mkdir(parents=True)
        (capture / "capture.json").write_text(
            json.dumps(
                {
                    "capture_id": capture.name,
                    "collection_id": "test-collection",
                    "status": "complete",
                    "selected_sources": [["web"]],
                    "sources": [{"source_id": "web", "status": "complete"}],
                }
            ),
            encoding="utf-8",
        )
        (metadata / "resolved-collection.json").write_text(
            json.dumps({"sources": [{"id": "web"}]}),
            encoding="utf-8",
        )
        finalize_capture(capture)

        self.assertFalse(_valid_pointer_target(capture, source_id=None))

    @patch(
        "text_preserver.capture.execute._run_wget",
        return_value=subprocess.CompletedProcess([], 0, "", "non-fatal warning"),
    )
    @patch(
        "text_preserver.capture.execute._validated_wget",
        return_value=("/usr/bin/wget", "GNU Wget test"),
    )
    def test_successful_source_with_warnings_updates_source_pointer(
        self,
        _mock_validated_wget: object,
        _mock_run_wget: object,
    ) -> None:
        config = load_config(self.config_path)
        collection_root = config.project.archive_root / "collections/test-collection"
        invalid_future = collection_root / "captures/20260829T120000Z-c1d2e3"
        invalid_future.mkdir(parents=True)
        (collection_root / "LATEST-web").write_text(
            "captures/20260829T120000Z-c1d2e3\n",
            encoding="utf-8",
        )

        result = execute_capture(
            config,
            "test-collection",
            source_ids=["web"],
            capture_id="20260828T120000Z-z1c2d3",
        )

        self.assertEqual(result.status, "complete_with_warnings")
        self.assertEqual(result.source_latest_updated, ("web",))
        self.assertEqual(
            (result.capture_directory.parent.parent / "LATEST-web")
            .read_text(encoding="utf-8")
            .strip(),
            "captures/20260828T120000Z-z1c2d3",
        )
        self.assertFalse((result.capture_directory.parent.parent / "LATEST").exists())

        same_second = execute_capture(
            config,
            "test-collection",
            source_ids=["web"],
            capture_id="20260828T120000Z-a1b2c3",
        )

        self.assertEqual(same_second.source_latest_updated, ("web",))
        self.assertEqual(
            (result.capture_directory.parent.parent / "LATEST-web")
            .read_text(encoding="utf-8")
            .strip(),
            "captures/20260828T120000Z-a1b2c3",
        )

        older = execute_capture(
            config,
            "test-collection",
            source_ids=["web"],
            capture_id="20260827T120000Z-a1b2c3",
        )

        self.assertEqual(older.source_latest_updated, ())
        self.assertEqual(
            (result.capture_directory.parent.parent / "LATEST-web")
            .read_text(encoding="utf-8")
            .strip(),
            "captures/20260828T120000Z-a1b2c3",
        )

    def test_failed_process_is_preserved_in_status_records(self) -> None:
        adapter = self.root / "inventory.py"
        adapter.write_text("def analyze_capture(*args, **kwargs): return {}\n", encoding="utf-8")
        invalid = VALID_CONFIG.replace(
            "[[collections.sources]]",
            '[collections.analysis]\ninventory_adapter = "inventory.py"\n\n[[collections.sources]]',
            1,
        ).replace(
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
        self.assertFalse((result.capture_directory.parent.parent / "LATEST-web").exists())
        self.assertEqual(
            (result.capture_directory / "metadata/input-config.toml").read_bytes(),
            config.input_bytes,
        )
        self.assertEqual(
            (result.capture_directory / "metadata/recipe-assets/inventory.py").read_bytes(),
            adapter.read_bytes(),
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
        self.assertFalse((capture_directory.parent.parent / "LATEST-web").exists())


if __name__ == "__main__":
    unittest.main()
