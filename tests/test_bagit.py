from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import text_preserver.preservation.bagit as bagit_module
from text_preserver.preservation.bagit import BagItError, create_bag, validate_bag
from text_preserver.preservation.fixity import finalize_capture
from text_preserver.preservation.payload_roles import (
    ExportPolicy,
    export_policy,
    load_verified_capture,
)


def finalized_capture(root: Path) -> Path:
    capture = root / "capture"
    metadata = capture / "metadata"
    metadata.mkdir(parents=True)
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
    (metadata / "public.json").write_bytes(b'{"title":"Public"}\n')
    (metadata / "private.txt").write_bytes(b"operator@example.test\n")
    (capture / "empty/preserved/directory").mkdir(parents=True)
    finalize_capture(capture)
    return capture


class BagItTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.capture_path = finalized_capture(self.root)
        self.capture = load_verified_capture(self.capture_path)

    def test_private_bag_preserves_every_capture_file_and_validates(self) -> None:
        output = self.root / "private-bag"

        result = create_bag(
            self.capture,
            output,
            profile="private",
            policy=ExportPolicy("private-complete-v1", None),
        )

        self.assertEqual(result.payload_files, len(self.capture.files))
        for relative in self.capture.files:
            self.assertEqual(
                (output / "data/capture" / relative).read_bytes(),
                (self.capture_path / relative).read_bytes(),
            )
        self.assertTrue((output / "data/capture/empty/preserved/directory").is_dir())
        validation = validate_bag(output)
        self.assertTrue(validation.ok, validation.errors)
        self.assertTrue((output / "manifest-sha256.txt").is_file())
        self.assertTrue((output / "manifest-sha512.txt").is_file())

    def test_public_bag_exports_only_explicitly_allowlisted_exact_bytes(self) -> None:
        allowed = frozenset({"capture.json", "metadata/public.json"})
        output = self.root / "public-bag"

        create_bag(
            self.capture,
            output,
            profile="public",
            policy=ExportPolicy("reviewed-public-v1", allowed),
        )

        self.assertEqual((output / "data/capture/metadata/public.json").read_bytes(), b'{"title":"Public"}\n')
        self.assertFalse((output / "data/capture/metadata/private.txt").exists())
        mapping = json.loads((output / "text-preserver-export.json").read_text(encoding="utf-8"))
        self.assertEqual({item["capture_path"] for item in mapping["files"]}, set(allowed))
        self.assertTrue(validate_bag(output).ok)

    def test_public_profile_refuses_implicit_export_all_policy(self) -> None:
        with self.assertRaisesRegex(BagItError, "explicit path allowlist"):
            create_bag(
                self.capture,
                self.root / "unsafe-public",
                profile="public",
                policy=ExportPolicy("unsafe", None),
            )
        self.assertFalse((self.root / "unsafe-public").exists())

    def test_builtin_public_policy_excludes_private_provenance_and_logs(self) -> None:
        capture_path = self.root / "schema3"
        files = {
            "metadata/environment.json": b'{"wget":"1.0"}\n',
            "metadata/private/operator.json": b'{"operator":"Private Name","note":"Secret note"}\n',
            "sources/web/logs/wget.log": b"private machine path\n",
            "sources/web/metadata/result.json": b'{"status":"complete"}\n',
            "sources/web/mirror/example.test/index.html": b"<h1>Mirror</h1>",
            "sources/web/warc/capture.warc.gz": b"warc bytes",
        }
        for relative, value in files.items():
            path = capture_path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
        roles = {
            "capture.json": "metadata",
            "manifest-sha256.json": "metadata",
            "SHA256SUMS": "metadata",
            **{
                path: (
                    "preservation_original"
                    if path.endswith(".warc.gz")
                    else "capture_derivative"
                    if "/mirror/" in path
                    else "metadata"
                )
                for path in files
            },
        }
        (capture_path / "capture.json").write_text(
            json.dumps(
                {
                    "schema_version": 3,
                    "capture_id": "20260829T120000Z-a1b2c3",
                    "collection_id": "fixture",
                    "status": "complete",
                    "payload_roles": [
                        {"path": path, "role": role} for path, role in sorted(roles.items())
                    ],
                }
            ),
            encoding="utf-8",
        )
        finalize_capture(capture_path)
        capture = load_verified_capture(capture_path)
        output = self.root / "builtin-public"

        create_bag(
            capture,
            output,
            profile="public",
            policy=export_policy(capture, "public"),
        )

        self.assertFalse((output / "data/capture/metadata/private/operator.json").exists())
        self.assertFalse((output / "data/capture/sources/web/logs/wget.log").exists())
        self.assertTrue((output / "data/capture/sources/web/warc/capture.warc.gz").is_file())
        self.assertTrue((output / "data/capture/sources/web/mirror/example.test/index.html").is_file())
        exported_bytes = b"".join(path.read_bytes() for path in output.rglob("*") if path.is_file())
        self.assertNotIn(b"Private Name", exported_bytes)
        self.assertNotIn(b"Secret note", exported_bytes)
        self.assertNotIn(b"private machine path", exported_bytes)
        self.assertNotIn(b"metadata/private/operator.json", exported_bytes)
        self.assertTrue(validate_bag(output).ok)

    def test_schema3_rejects_payload_role_on_private_metadata(self) -> None:
        capture = self.root / "misclassified"
        private = capture / "metadata/private/operator.json"
        private.parent.mkdir(parents=True)
        private.write_text('{"operator":"Private"}', encoding="utf-8")
        paths = {
            "capture.json": "metadata",
            "metadata/private/operator.json": "preservation_original",
            "manifest-sha256.json": "metadata",
            "SHA256SUMS": "metadata",
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

        with self.assertRaisesRegex(ValueError, "not permitted for metadata path"):
            load_verified_capture(capture)

    def test_independent_validation_detects_payload_and_tag_tampering(self) -> None:
        output = self.root / "tampered-bag"
        create_bag(
            self.capture,
            output,
            profile="private",
            policy=ExportPolicy("private-complete-v1", None),
        )
        (output / "data/capture/metadata/public.json").write_bytes(b"changed")
        (output / "bag-info.txt").write_text("Payload-Oxum: 1.1\n", encoding="utf-8")

        validation = validate_bag(output)

        self.assertFalse(validation.ok)
        self.assertTrue(any("checksum mismatch" in error or "sha256 mismatch" in error for error in validation.errors))
        self.assertTrue(any("Payload-Oxum mismatch" in error for error in validation.errors))

    def test_creation_never_replaces_existing_destination(self) -> None:
        output = self.root / "existing"
        output.mkdir()
        marker = output / "marker"
        marker.write_text("keep", encoding="utf-8")

        with self.assertRaisesRegex(BagItError, "already exists"):
            create_bag(
                self.capture,
                output,
                profile="private",
                policy=ExportPolicy("private-complete-v1", None),
            )

        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_transient_source_mutation_cannot_enter_bag(self) -> None:
        output = self.root / "transient"
        source = self.capture_path / "metadata/public.json"
        original_copy = bagit_module._copy_regular

        def changing_copy(source_path: Path, destination_path: Path) -> None:
            if source_path.resolve() == source.resolve():
                original = source_path.read_bytes()
                source_path.write_bytes(b"changed during copy")
                try:
                    original_copy(source_path, destination_path)
                finally:
                    source_path.write_bytes(original)
            else:
                original_copy(source_path, destination_path)

        with patch.object(bagit_module, "_copy_regular", side_effect=changing_copy):
            with self.assertRaisesRegex(BagItError, "changed while exporting"):
                create_bag(
                    self.capture,
                    output,
                    profile="private",
                    policy=ExportPolicy("private-complete-v1", None),
                )

        self.assertFalse(output.exists())
        self.assertEqual(source.read_bytes(), b'{"title":"Public"}\n')


if __name__ == "__main__":
    unittest.main()
