from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from text_preserver.preservation.capture import CaptureExecutionError, execute_capture
from text_preserver.preservation.capture.execute import (
    _aggregate_status,
    _valid_pointer_target,
    collection_lock,
    termination_signals_as_interrupts,
)
from text_preserver.config import load_config
from text_preserver.preservation.fixity import finalize_capture

from tests.test_config import VALID_CONFIG


CDX_HEADER = b" CDX N b a m s k r M S V g\n"


def wget_with_mirror(
    command: object,
    *,
    returncode: int = 0,
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    working_directory = getattr(command, "working_directory")
    payload = working_directory / "mirror/example.org/payload.txt"
    payload.parent.mkdir(parents=True, exist_ok=True)
    payload.write_bytes(b"payload")
    return subprocess.CompletedProcess([], returncode, "", stderr)


def wget_with_warning(command: object) -> subprocess.CompletedProcess[str]:
    return wget_with_mirror(command, stderr="non-fatal warning")


def wget_with_partial_mirror(command: object) -> subprocess.CompletedProcess[str]:
    return wget_with_mirror(command, returncode=4)


def wget_then_interrupt(command: object) -> subprocess.CompletedProcess[str]:
    wget_with_mirror(command)
    raise KeyboardInterrupt


def wget_then_error(command: object) -> subprocess.CompletedProcess[str]:
    wget_with_mirror(command)
    raise OSError("mock execution error")


def write_warc(path: Path, *record_types: str) -> None:
    records = []
    for record_type in record_types:
        records.append(
            b"WARC/1.0\r\n"
            + f"WARC-Type: {record_type}\r\n".encode("ascii")
            + b"Content-Length: 0\r\n\r\n\r\n"
        )
    with gzip.open(path, "wb") as stream:
        stream.write(b"".join(records))


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

    def test_aggregate_status_uses_structured_and_legacy_payloads(self) -> None:
        structured = {
            "required": True,
            "status": "failed",
            "payloads": {
                "mirror": {"files": 0, "bytes": 0},
                "warc": {"has_response_or_resource": True},
            },
            "downloaded_files": 0,
            "downloaded_bytes": 0,
        }
        legacy = {
            "required": True,
            "status": "failed",
            "downloaded_files": 1,
            "downloaded_bytes": 7,
        }

        self.assertEqual(_aggregate_status([structured]), "partial")
        self.assertEqual(_aggregate_status([legacy]), "partial")

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

    def test_full_pointer_target_requires_successful_records_for_all_sources(self) -> None:
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
                    "selected_sources": ["web", "dataset"],
                    "sources": [{"source_id": "web", "status": "complete"}],
                }
            ),
            encoding="utf-8",
        )
        (metadata / "resolved-collection.json").write_text(
            json.dumps({"sources": [{"id": "web"}, {"id": "dataset"}]}),
            encoding="utf-8",
        )
        finalize_capture(capture)

        self.assertFalse(_valid_pointer_target(capture, source_id=None))

    @patch(
        "text_preserver.preservation.capture.execute._run_wget",
        side_effect=wget_with_warning,
    )
    @patch(
        "text_preserver.preservation.capture.execute._validated_wget",
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

    @patch(
        "text_preserver.preservation.capture.execute._run_wget",
        side_effect=wget_with_mirror,
    )
    @patch(
        "text_preserver.preservation.capture.execute._validated_wget",
        return_value=("/usr/bin/wget", "GNU Wget test"),
    )
    def test_mirror_payload_metrics_and_aliases(
        self,
        _mock_validated_wget: object,
        _mock_run_wget: object,
    ) -> None:
        result = execute_capture(
            load_config(self.config_path),
            "test-collection",
            source_ids=["web"],
            capture_id="20260827T120000Z-m1r2r3",
        )

        source = result.metadata["sources"][0]
        self.assertEqual(result.metadata["schema_version"], 2)
        self.assertEqual(result.status, "complete")
        self.assertEqual(source["payloads"]["mirror"], {"files": 1, "bytes": 7})
        self.assertEqual(source["downloaded_files"], 1)
        self.assertEqual(source["downloaded_bytes"], 7)
        self.assertEqual(source["payloads"]["warc"]["files"], 0)
        self.assertIsNone(source["payloads"]["warc"]["indexed_records"])

    @patch(
        "text_preserver.preservation.capture.execute._validated_wget",
        return_value=("/usr/bin/wget", "GNU Wget test"),
    )
    def test_header_only_cdx_and_metadata_warc_are_not_substantive(
        self,
        _mock_validated_wget: object,
    ) -> None:
        def mock_wget(command: object) -> subprocess.CompletedProcess[str]:
            warc_root = getattr(command, "working_directory") / "warc"
            write_warc(warc_root / "capture.warc.gz", "warcinfo", "metadata")
            (warc_root / "capture.cdx").write_bytes(CDX_HEADER)
            (warc_root / "tmp/temporary.warc.gz").write_bytes(b"temporary")
            return subprocess.CompletedProcess([], 0, "", "")

        with patch(
            "text_preserver.preservation.capture.execute._run_wget",
            side_effect=mock_wget,
        ):
            result = execute_capture(
                load_config(self.config_path),
                "test-collection",
                source_ids=["web"],
                capture_id="20260827T120000Z-h1d2r3",
            )

        source = result.metadata["sources"][0]
        warc = source["payloads"]["warc"]
        self.assertEqual(result.status, "failed")
        self.assertEqual(warc["files"], 1)
        self.assertGreater(warc["bytes"], 0)
        self.assertEqual(warc["cdx_files"], 1)
        self.assertEqual(warc["indexed_records"], 0)
        self.assertFalse(warc["has_response_or_resource"])
        self.assertIn("no mirror files or WARC response/resource evidence", source["error"])

    @patch(
        "text_preserver.preservation.capture.execute._validated_wget",
        return_value=("/usr/bin/wget", "GNU Wget test"),
    )
    def test_warc_only_cdx_records_are_substantive(
        self,
        _mock_validated_wget: object,
    ) -> None:
        self.config_path.write_text(
            VALID_CONFIG.replace(
                'quota = "50M"',
                'quota = "50M"\nmirror = false\npage_requisites = false\n'
                "convert_links = false\nadjust_extension = false",
            ),
            encoding="utf-8",
        )

        def mock_wget(command: object) -> subprocess.CompletedProcess[str]:
            warc_root = getattr(command, "working_directory") / "warc"
            write_warc(warc_root / "capture.warc.gz", "warcinfo", "response")
            (warc_root / "capture.cdx").write_bytes(
                CDX_HEADER + b"example record one\nexample record two\n"
            )
            return subprocess.CompletedProcess([], 0, "", "")

        with patch(
            "text_preserver.preservation.capture.execute._run_wget",
            side_effect=mock_wget,
        ):
            result = execute_capture(
                load_config(self.config_path),
                "test-collection",
                source_ids=["web"],
                capture_id="20260827T120000Z-w1r2c3",
            )

        source = result.metadata["sources"][0]
        warc = source["payloads"]["warc"]
        self.assertEqual(result.status, "complete")
        self.assertEqual(source["downloaded_files"], 0)
        self.assertEqual(source["downloaded_bytes"], 0)
        self.assertEqual(warc["files"], 1)
        self.assertEqual(warc["cdx_files"], 1)
        self.assertEqual(warc["indexed_records"], 2)
        self.assertTrue(warc["has_response_or_resource"])

    @patch(
        "text_preserver.preservation.capture.execute._validated_wget",
        return_value=("/usr/bin/wget", "GNU Wget test"),
    )
    def test_warc_without_cdx_uses_bounded_record_header_inspection(
        self,
        _mock_validated_wget: object,
    ) -> None:
        self.config_path.write_text(
            VALID_CONFIG.replace(
                'quota = "50M"',
                'quota = "50M"\nmirror = false\nwarc_cdx = false\n'
                "page_requisites = false\nconvert_links = false\nadjust_extension = false",
            ),
            encoding="utf-8",
        )

        def mock_wget(command: object) -> subprocess.CompletedProcess[str]:
            write_warc(
                getattr(command, "working_directory") / "warc/capture.warc.gz",
                "warcinfo",
                "resource",
            )
            return subprocess.CompletedProcess([], 0, "", "")

        with patch(
            "text_preserver.preservation.capture.execute._run_wget",
            side_effect=mock_wget,
        ):
            result = execute_capture(
                load_config(self.config_path),
                "test-collection",
                source_ids=["web"],
                capture_id="20260827T120000Z-n1c2x3",
            )

        warc = result.metadata["sources"][0]["payloads"]["warc"]
        self.assertEqual(result.status, "complete")
        self.assertIsNone(warc["indexed_records"])
        self.assertTrue(warc["has_response_or_resource"])

    @patch(
        "text_preserver.preservation.capture.execute._run_wget",
        side_effect=wget_with_partial_mirror,
    )
    @patch(
        "text_preserver.preservation.capture.execute._validated_wget",
        return_value=("/usr/bin/wget", "GNU Wget test"),
    )
    def test_nonaccepted_exit_with_payload_is_partial(
        self,
        _mock_validated_wget: object,
        _mock_run_wget: object,
    ) -> None:
        result = execute_capture(
            load_config(self.config_path),
            "test-collection",
            source_ids=["web"],
            capture_id="20260827T120000Z-p1r2t3",
        )

        self.assertEqual(result.status, "partial")
        self.assertEqual(result.metadata["sources"][0]["payloads"]["mirror"]["files"], 1)

    def test_failed_process_is_preserved_in_status_records(self) -> None:
        adapter = self.root / "inventory.py"
        adapter.write_text("def analyze_capture(*args, **kwargs): return {}\n", encoding="utf-8")
        reader = self.root / "reader.py"
        reader.write_text("def render_static_reader(*args, **kwargs): return {}\n", encoding="utf-8")
        invalid = VALID_CONFIG.replace(
            "[[collections.sources]]",
            '[collections.analysis]\ninventory_adapter = "inventory.py"\nreader_adapter = "reader.py"\n\n[[collections.sources]]',
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
            (result.capture_directory / "metadata/recipe-bundle/inventory.py").read_bytes(),
            adapter.read_bytes(),
        )
        self.assertEqual(
            (result.capture_directory / "metadata/recipe-bundle/reader.py").read_bytes(),
            reader.read_bytes(),
        )
        bundle_manifest = json.loads(
            (result.capture_directory / "metadata/recipe-bundle-manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertIsNone(bundle_manifest["recipe_api"])
        self.assertFalse(
            (result.capture_directory / "metadata/recipe-bundle/collections.toml").exists()
        )

    @patch(
        "text_preserver.preservation.capture.execute._run_wget",
        side_effect=wget_with_mirror,
    )
    @patch(
        "text_preserver.preservation.capture.execute._validated_wget",
        return_value=("/usr/bin/wget", "GNU Wget test"),
    )
    def test_recipe_capture_preserves_complete_recursive_bundle(
        self,
        _mock_validated_wget: object,
        _mock_run_wget: object,
    ) -> None:
        recipe = self.root / "recipe"
        (recipe / "fixtures").mkdir(parents=True)
        (recipe / "templates").mkdir()
        (recipe / "__pycache__").mkdir()
        (recipe / "collection.toml").write_text(
            """
recipe_api = 1

[collection]
id = "bundled"
title = "Bundled Recipe"

[collection.analysis]
inventory_adapter = "inventory.py"

[[collection.sources]]
id = "data"
kind = "http-file"
title = "Data"
seeds = ["https://example.org/data.txt"]
allowed_hosts = ["example.org"]

[collection.sources.capture]
recursive = false
page_requisites = false
convert_links = false
adjust_extension = false
""".strip(),
            encoding="utf-8",
        )
        (recipe / "inventory.py").write_text("VALUE = 1\n", encoding="utf-8")
        (recipe / "fixtures/example.txt").write_text("fixture\n", encoding="utf-8")
        (recipe / "templates/page.html").write_text("<p>template</p>\n", encoding="utf-8")
        (recipe / "README.md").write_text("recipe documentation\n", encoding="utf-8")
        (recipe / "ignored.pyc").write_bytes(b"transient")
        (recipe / "__pycache__/ignored.py").write_text("transient\n", encoding="utf-8")
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

        result = execute_capture(
            load_config(self.config_path),
            "bundled",
            capture_id="20260827T120000Z-b1c2d3",
        )

        bundle = result.capture_directory / "metadata/recipe-bundle"
        self.assertEqual((bundle / "fixtures/example.txt").read_text(), "fixture\n")
        self.assertTrue((bundle / "templates/page.html").is_file())
        self.assertTrue((bundle / "README.md").is_file())
        self.assertFalse((bundle / "ignored.pyc").exists())
        self.assertFalse((bundle / "__pycache__").exists())
        manifest = json.loads(
            (result.capture_directory / "metadata/recipe-bundle-manifest.json").read_text(
                encoding="utf-8"
            )
        )
        paths = [entry["path"] for entry in manifest["files"]]
        self.assertEqual(paths, sorted(paths))
        self.assertEqual(manifest["recipe_api"], 1)
        self.assertIn("fixtures/example.txt", paths)
        self.assertIn("templates/page.html", paths)

    @patch(
        "text_preserver.preservation.capture.execute._run_wget",
        side_effect=wget_then_interrupt,
    )
    @patch(
        "text_preserver.preservation.capture.execute._validated_wget",
        return_value=("/usr/bin/wget", "GNU Wget test"),
    )
    def test_interruption_is_preserved_at_source_and_capture_level(
        self,
        _mock_validated_wget: object,
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
        self.assertEqual(source["payloads"]["mirror"], {"files": 1, "bytes": 7})
        self.assertEqual(source["downloaded_files"], 1)
        self.assertFalse((capture_directory.parent.parent / "LATEST").exists())
        self.assertFalse((capture_directory.parent.parent / "LATEST-web").exists())

    @patch(
        "text_preserver.preservation.capture.execute._run_wget",
        side_effect=wget_then_error,
    )
    @patch(
        "text_preserver.preservation.capture.execute._validated_wget",
        return_value=("/usr/bin/wget", "GNU Wget test"),
    )
    def test_execution_error_records_retained_payload_metrics(
        self,
        _mock_validated_wget: object,
        _mock_run: object,
    ) -> None:
        result = execute_capture(
            load_config(self.config_path),
            "test-collection",
            source_ids=["web"],
            capture_id="20260827T120000Z-e1r2r3",
        )

        source = result.metadata["sources"][0]
        self.assertEqual(result.status, "partial")
        self.assertEqual(source["status"], "partial")
        self.assertEqual(source["payloads"]["mirror"], {"files": 1, "bytes": 7})
        self.assertEqual(source["error"], "mock execution error")


if __name__ == "__main__":
    unittest.main()
