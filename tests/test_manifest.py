from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest

from text_preserver.cli import main
from text_preserver.preservation.fixity import ManifestError, finalize_capture, verify_capture


class ManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.capture = Path(self.temporary_directory.name) / "capture"
        (self.capture / "metadata").mkdir(parents=True)
        (self.capture / "capture.json").write_text('{"status":"complete"}\n', encoding="utf-8")
        (self.capture / "metadata/source.json").write_text("source\n", encoding="utf-8")

    def finalize(self) -> dict[str, object]:
        return finalize_capture(self.capture)

    def test_finalized_capture_verifies_and_includes_sums(self) -> None:
        manifest = self.finalize()

        paths = {entry["path"] for entry in manifest["files"]}  # type: ignore[index]
        self.assertIn("SHA256SUMS", paths)
        self.assertNotIn("manifest-sha256.json", paths)
        result = verify_capture(self.capture)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.checked_objects, len(paths))

    def test_finalization_is_single_use(self) -> None:
        self.finalize()

        with self.assertRaisesRegex(ManifestError, "already finalized"):
            finalize_capture(self.capture)

    def test_partial_sums_output_can_be_recovered_before_commit(self) -> None:
        (self.capture / "SHA256SUMS").write_text("stale\n", encoding="utf-8")

        self.finalize()

        self.assertTrue(verify_capture(self.capture).ok)

    def test_verification_detects_modified_missing_and_unexpected_objects(self) -> None:
        self.finalize()
        source = self.capture / "metadata/source.json"
        source.write_text("modified\n", encoding="utf-8")
        (self.capture / "unexpected.txt").write_text("unexpected\n", encoding="utf-8")
        (self.capture / "capture.json").unlink()

        result = verify_capture(self.capture)

        self.assertFalse(result.ok)
        self.assertTrue(any("source.json: size mismatch" in error for error in result.errors))
        self.assertIn("missing object: capture.json", result.errors)
        self.assertIn("unexpected object: unexpected.txt", result.errors)

    def test_verification_detects_unexpected_empty_directory(self) -> None:
        self.finalize()
        (self.capture / "empty-added-later").mkdir()

        result = verify_capture(self.capture)

        self.assertIn("unexpected object: empty-added-later", result.errors)

    def test_finalization_rejects_symlinks(self) -> None:
        target = self.capture / "metadata/source.json"
        (self.capture / "linked-source").symlink_to(target)

        with self.assertRaisesRegex(ManifestError, "symlinks are not allowed"):
            self.finalize()

    def test_verification_rejects_symlinked_manifest(self) -> None:
        self.finalize()
        manifest = self.capture / "manifest-sha256.json"
        external = Path(self.temporary_directory.name) / "external.json"
        external.write_bytes(manifest.read_bytes())
        manifest.unlink()
        manifest.symlink_to(external)

        result = verify_capture(self.capture)

        self.assertFalse(result.ok)
        self.assertIn("must not be a symlink", result.errors[0])

    def test_verification_rejects_unsafe_manifest_paths(self) -> None:
        self.finalize()
        path = self.capture / "manifest-sha256.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["files"][0]["path"] = "../escape"
        path.write_text(json.dumps(manifest), encoding="utf-8")

        result = verify_capture(self.capture)

        self.assertFalse(result.ok)
        self.assertIn("unsafe path", result.errors[0])

    def test_cli_verify_supports_json(self) -> None:
        self.finalize()
        output = StringIO()

        with redirect_stdout(output):
            exit_code = main(["verify", str(self.capture), "--json"])

        self.assertEqual(exit_code, 0)
        self.assertTrue(json.loads(output.getvalue())["ok"])

    def test_collection_verification_prefers_canonical_and_falls_back_to_legacy(self) -> None:
        collection = Path(self.temporary_directory.name) / "collection"
        capture_id = "20260829T120000Z-a1b2c3"
        capture = collection / "captures" / capture_id
        (capture / "metadata").mkdir(parents=True)
        (capture / "capture.json").write_text("{}\n", encoding="utf-8")
        finalize_capture(capture)
        (collection / "LATEST").write_text(f"captures/{capture_id}\n", encoding="utf-8")

        self.assertTrue(verify_capture(collection).ok)
        (collection / "LATEST-ACQUIRED").write_text("../escape\n", encoding="utf-8")

        result = verify_capture(collection)
        self.assertFalse(result.ok)
        self.assertIn("unsafe LATEST-ACQUIRED", result.errors[0])

    def test_collection_verification_accepts_long_capture_suffix(self) -> None:
        collection = Path(self.temporary_directory.name) / "collection"
        capture_id = "20260829T120000Z-a1b2c3d4e5f6"
        capture = collection / "captures" / capture_id
        (capture / "metadata").mkdir(parents=True)
        (capture / "capture.json").write_text("{}\n", encoding="utf-8")
        finalize_capture(capture)
        (collection / "LATEST-ACQUIRED").write_text(
            f"captures/{capture_id}\n", encoding="utf-8"
        )

        self.assertTrue(verify_capture(collection).ok)


if __name__ == "__main__":
    unittest.main()
