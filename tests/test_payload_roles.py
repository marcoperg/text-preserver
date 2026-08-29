from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from text_preserver.preservation.fixity import finalize_capture
from text_preserver.preservation.payload_roles import (
    PayloadRole,
    PayloadRoleError,
    load_verified_capture,
)


class PayloadRoleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)

    def test_schema2_infers_warc_web_mirror_direct_deposit_and_metadata(self) -> None:
        capture = self.root / "capture"
        for source_id, kind in (("web", "web"), ("deposit", "http-file")):
            metadata = capture / f"sources/{source_id}/metadata"
            metadata.mkdir(parents=True)
            (metadata / "resolved-source.json").write_text(
                json.dumps({"id": source_id, "kind": kind}), encoding="utf-8"
            )
            mirror = capture / f"sources/{source_id}/mirror/example.test"
            mirror.mkdir(parents=True)
            (mirror / "payload.txt").write_bytes(source_id.encode("ascii"))
        warc = capture / "sources/web/warc"
        warc.mkdir()
        (warc / "capture.warc.gz").write_bytes(b"warc bytes")
        (warc / "capture.cdx").write_bytes(b"index")
        (capture / "capture.json").write_text(
            json.dumps({"schema_version": 2, "status": "complete"}), encoding="utf-8"
        )
        finalize_capture(capture)

        verified = load_verified_capture(capture)

        self.assertEqual(
            verified.role_for("sources/web/warc/capture.warc.gz"),
            PayloadRole.PRESERVATION_ORIGINAL,
        )
        self.assertEqual(
            verified.role_for("sources/web/warc/capture.cdx"),
            PayloadRole.CAPTURE_DERIVATIVE,
        )
        self.assertEqual(
            verified.role_for("sources/web/mirror/example.test/payload.txt"),
            PayloadRole.CAPTURE_DERIVATIVE,
        )
        self.assertEqual(
            verified.role_for("sources/deposit/mirror/example.test/payload.txt"),
            PayloadRole.PRESERVATION_ORIGINAL,
        )
        self.assertEqual(verified.role_for("capture.json"), PayloadRole.METADATA)
        self.assertEqual(verified.role_for("manifest-sha256.json"), PayloadRole.METADATA)

    def test_schema3_requires_exact_explicit_file_coverage(self) -> None:
        capture = self.root / "schema3"
        payload = capture / "sources/web/warc/capture.warc"
        payload.parent.mkdir(parents=True)
        payload.write_bytes(b"WARC/1.1\r\nContent-Length: 0\r\n\r\n\r\n")
        paths = {
            "capture.json": "metadata",
            "sources/web/warc/capture.warc": "preservation_original",
            "SHA256SUMS": "metadata",
            "manifest-sha256.json": "metadata",
        }
        (capture / "capture.json").write_text(
            json.dumps(
                {
                    "schema_version": 3,
                    "status": "complete",
                    "payload_roles": [
                        {"path": path, "role": role} for path, role in sorted(paths.items())
                    ],
                }
            ),
            encoding="utf-8",
        )
        finalize_capture(capture)

        verified = load_verified_capture(capture)

        self.assertEqual(verified.schema_version, 3)
        self.assertEqual(verified.files, frozenset(paths))

    def test_schema3_rejects_missing_and_unknown_roles(self) -> None:
        capture = self.root / "invalid"
        capture.mkdir()
        (capture / "capture.json").write_text(
            json.dumps(
                {
                    "schema_version": 3,
                    "status": "complete",
                    "payload_roles": [
                        {"path": "capture.json", "role": "metadata"},
                        {"path": "SHA256SUMS", "role": "metadata"},
                        {"path": "manifest-sha256.json", "role": "not-a-role"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        finalize_capture(capture)

        with self.assertRaisesRegex(PayloadRoleError, "unsupported role"):
            load_verified_capture(capture)

    def test_legacy_mirror_without_source_metadata_is_ambiguous(self) -> None:
        capture = self.root / "ambiguous"
        mirror = capture / "sources/source/mirror/example.test"
        mirror.mkdir(parents=True)
        (mirror / "file.txt").write_text("payload", encoding="utf-8")
        (capture / "capture.json").write_text(
            json.dumps({"schema_version": 1, "status": "complete"}), encoding="utf-8"
        )
        finalize_capture(capture)

        with self.assertRaisesRegex(PayloadRoleError, "without metadata"):
            load_verified_capture(capture)


if __name__ == "__main__":
    unittest.main()
