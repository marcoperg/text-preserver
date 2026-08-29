from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import text_preserver.access.wacz as wacz_module
from text_preserver.access.wacz import (
    WaczError,
    WaczMetadata,
    create_wacz,
    validate_wacz,
)
from text_preserver.preservation.fixity import finalize_capture
from text_preserver.preservation.payload_roles import ExportPolicy, load_verified_capture


def warc_record(record_type: str, payload: bytes, **headers: str) -> bytes:
    values = {
        "WARC-Type": record_type,
        "WARC-Record-ID": f"<urn:uuid:{record_type}-record>",
        **headers,
        "Content-Length": str(len(payload)),
    }
    header = b"WARC/1.1\r\n" + b"".join(
        f"{name}: {value}\r\n".encode("utf-8") for name, value in values.items()
    )
    return header + b"\r\n" + payload + b"\r\n\r\n"


def finalized_warc_capture(root: Path) -> tuple[Path, str, bytes]:
    capture = root / "capture"
    warc_path = "sources/web/warc/capture.warc.gz"
    warc = capture / warc_path
    warc.parent.mkdir(parents=True)
    warcinfo = warc_record("warcinfo", b"software: synthetic\r\n")
    metadata_resource = warc_record(
        "resource",
        b"--warc-file=synthetic\n",
        **{
            "WARC-Target-URI": "metadata://gnu.org/software/wget/warc/wget_arguments.txt",
            "WARC-Date": "2026-08-29T12:00:00Z",
        },
    )
    http = b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=UTF-8\r\n\r\n<h1>Hello</h1>"
    response = warc_record(
        "response",
        http,
        **{
            "WARC-Target-URI": "<https://example.test/hello>",
            "WARC-Date": "2026-08-29T12:00:00Z",
        },
    )
    second_response = warc_record(
        "response",
        b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\nSecond",
        **{
            "WARC-Target-URI": "https://example.test/second",
            "WARC-Date": "2026-08-29T12:00:01Z",
            "WARC-Record-ID": "<urn:uuid:second-response-record>",
        },
    )
    source_bytes = (
        gzip.compress(warcinfo, mtime=0)
        + gzip.compress(metadata_resource, mtime=0)
        + gzip.compress(response, mtime=0)
        + gzip.compress(second_response, mtime=0)
    )
    warc.write_bytes(source_bytes)
    (capture / "capture.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "capture_id": "20260829T120000Z-a1b2c3",
                "status": "complete",
            }
        ),
        encoding="utf-8",
    )
    finalize_capture(capture)
    return capture, warc_path, source_bytes


class WaczTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.capture_path, self.warc_path, self.source_bytes = finalized_warc_capture(self.root)
        self.capture = load_verified_capture(self.capture_path)
        self.metadata = WaczMetadata(
            title="Synthetic web archive",
            description="Offline test capture.",
            created="2026-08-29T12:01:00Z",
            main_page_url="https://example.test/hello",
            main_page_date="2026-08-29T12:00:00Z",
        )

    def test_wacz_contains_exact_warc_and_generated_replay_metadata(self) -> None:
        output = self.root / "capture.wacz"
        before = (self.capture_path / self.warc_path).stat()

        result = create_wacz(
            self.capture,
            output,
            warc_paths=[self.warc_path],
            profile="private",
            policy=ExportPolicy("private-replay-v1", None),
            metadata=self.metadata,
        )

        self.assertEqual(result.indexed_records, 2)
        self.assertEqual(result.pages, 2)
        with zipfile.ZipFile(output) as package:
            self.assertEqual(package.read("archive/capture.warc.gz"), self.source_bytes)
            cdx = gzip.decompress(package.read("indexes/index.cdx.gz")).decode("utf-8")
            self.assertIn("test,example)/hello 20260829120000", cdx)
            pages = package.read("pages/pages.jsonl").decode("utf-8").splitlines()
            self.assertEqual(json.loads(pages[0])["format"], "json-pages-1.0")
            self.assertEqual(json.loads(pages[1])["url"], "https://example.test/hello")
            datapackage = json.loads(package.read("datapackage.json"))
            self.assertEqual(datapackage["wacz_version"], "1.1.1")
        after = (self.capture_path / self.warc_path).stat()
        self.assertEqual((before.st_size, before.st_mtime_ns), (after.st_size, after.st_mtime_ns))
        self.assertEqual((self.capture_path / self.warc_path).read_bytes(), self.source_bytes)
        validation = validate_wacz(output)
        self.assertTrue(validation.ok, validation.errors)

    def test_public_wacz_requires_warc_to_be_explicitly_allowed(self) -> None:
        with self.assertRaisesRegex(WaczError, "not permitted"):
            create_wacz(
                self.capture,
                self.root / "denied.wacz",
                warc_paths=[self.warc_path],
                profile="public",
                policy=ExportPolicy("public-deny-v1", frozenset({"capture.json"})),
                metadata=self.metadata,
            )
        self.assertFalse((self.root / "denied.wacz").exists())

    def test_validator_detects_resource_tampering_without_source_capture(self) -> None:
        valid = self.root / "valid.wacz"
        tampered = self.root / "tampered.wacz"
        create_wacz(
            self.capture,
            valid,
            warc_paths=[self.warc_path],
            profile="private",
            policy=ExportPolicy("private-replay-v1", None),
            metadata=self.metadata,
        )
        with zipfile.ZipFile(valid) as source, zipfile.ZipFile(tampered, "w") as destination:
            for info in source.infolist():
                value = source.read(info)
                if info.filename == "pages/pages.jsonl":
                    value += b'{"url":"https://bad.test/","ts":"bad"}\n'
                destination.writestr(info, value)

        validation = validate_wacz(tampered)

        self.assertFalse(validation.ok)
        self.assertTrue(any("resource" in error or "page" in error for error in validation.errors))

    def test_validator_rejects_index_that_does_not_match_warc_record(self) -> None:
        valid = self.root / "valid-index.wacz"
        tampered = self.root / "tampered-index.wacz"
        create_wacz(
            self.capture,
            valid,
            warc_paths=[self.warc_path],
            profile="private",
            policy=ExportPolicy("private-replay-v1", None),
            metadata=self.metadata,
        )
        with zipfile.ZipFile(valid) as source:
            members = {info.filename: source.read(info) for info in source.infolist()}
            compression = {info.filename: info.compress_type for info in source.infolist()}
        cdx_lines = gzip.decompress(members["indexes/index.cdx.gz"]).decode("utf-8").splitlines()
        key, timestamp, raw_value = cdx_lines[0].split(" ", 2)
        value = json.loads(raw_value)
        value["url"] = "https://example.test/fabricated"
        cdx_lines[0] = (
            f"{key} {timestamp} {json.dumps(value, separators=(',', ':'), sort_keys=True)}"
        )
        changed_cdx = ("\n".join(cdx_lines) + "\n").encode("utf-8")
        members["indexes/index.cdx.gz"] = gzip.compress(changed_cdx, mtime=0)
        datapackage = json.loads(members["datapackage.json"])
        for resource in datapackage["resources"]:
            if resource["path"] == "indexes/index.cdx.gz":
                resource["bytes"] = len(members["indexes/index.cdx.gz"])
                resource["hash"] = "sha256:" + hashlib.sha256(
                    members["indexes/index.cdx.gz"]
                ).hexdigest()
        members["datapackage.json"] = (
            json.dumps(datapackage, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        members["datapackage-digest.json"] = (
            json.dumps(
                {
                    "hash": "sha256:"
                    + hashlib.sha256(members["datapackage.json"]).hexdigest(),
                    "path": "datapackage.json",
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        with zipfile.ZipFile(tampered, "w") as destination:
            for name, content in members.items():
                destination.writestr(name, content, compress_type=compression[name])

        validation = validate_wacz(tampered)

        self.assertFalse(validation.ok)
        self.assertTrue(any("does not match its WARC record" in error for error in validation.errors))

    def test_invalid_warc_leaves_no_partial_destination(self) -> None:
        broken_capture = self.root / "broken-capture"
        broken_warc_path = "sources/web/warc/broken.warc.gz"
        broken = broken_capture / broken_warc_path
        broken.parent.mkdir(parents=True)
        broken.write_bytes(b"not gzip")
        (broken_capture / "capture.json").write_text(
            json.dumps({"schema_version": 2, "status": "complete"}), encoding="utf-8"
        )
        finalize_capture(broken_capture)
        output = self.root / "broken.wacz"

        with self.assertRaisesRegex(WaczError, "invalid gzip WARC"):
            create_wacz(
                load_verified_capture(broken_capture),
                output,
                warc_paths=[broken_warc_path],
                profile="private",
                policy=ExportPolicy("private-replay-v1", None),
                metadata=self.metadata,
            )

        self.assertFalse(output.exists())

    def test_transient_source_mutation_cannot_enter_wacz(self) -> None:
        output = self.root / "transient.wacz"
        source = self.capture_path / self.warc_path
        original_copy = wacz_module._copy_verified_warc

        def changing_copy(source_path: Path, destination_path: Path, digest: str) -> None:
            original = source_path.read_bytes()
            source_path.write_bytes(gzip.compress(b"changed", mtime=0))
            try:
                original_copy(source_path, destination_path, digest)
            finally:
                source_path.write_bytes(original)

        with patch.object(wacz_module, "_copy_verified_warc", side_effect=changing_copy):
            with self.assertRaisesRegex(WaczError, "changed while staging"):
                create_wacz(
                    self.capture,
                    output,
                    warc_paths=[self.warc_path],
                    profile="private",
                    policy=ExportPolicy("private-replay-v1", None),
                    metadata=self.metadata,
                )

        self.assertFalse(output.exists())
        self.assertEqual(source.read_bytes(), self.source_bytes)


if __name__ == "__main__":
    unittest.main()
