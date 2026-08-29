from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from text_preserver.recipe_bundle import (
    RecipeBundleError,
    copy_bundle,
    scan_declared_assets,
    scan_recipe_directory,
    verify_bundle_manifest,
)


class RecipeBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name) / "recipe"
        self.root.mkdir()

    def test_digest_and_posix_order_are_deterministic(self) -> None:
        (self.root / "z.txt").write_text("last", encoding="utf-8")
        (self.root / "nested").mkdir()
        (self.root / "nested/a.txt").write_text("first", encoding="utf-8")

        first = scan_recipe_directory(self.root)
        second = scan_recipe_directory(self.root)

        self.assertEqual(first.sha256, second.sha256)
        self.assertEqual(
            [item.path for item in first.files],
            ["nested/a.txt", "z.txt"],
        )

    def test_excludes_only_documented_transient_cache_artifacts(self) -> None:
        (self.root / "__pycache__").mkdir()
        (self.root / "__pycache__/module.py").write_text("ignored", encoding="utf-8")
        (self.root / "module.pyc").write_bytes(b"ignored")
        (self.root / "module.pyo").write_bytes(b"ignored")
        (self.root / ".DS_Store").write_bytes(b"ignored")
        (self.root / ".cache").mkdir()
        (self.root / ".cache/kept.txt").write_text("kept", encoding="utf-8")

        bundle = scan_recipe_directory(self.root)

        self.assertEqual([item.path for item in bundle.files], [".cache/kept.txt"])

    def test_rejects_symlinks(self) -> None:
        outside = self.root.parent / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        (self.root / "link.txt").symlink_to(outside)

        with self.assertRaisesRegex(RecipeBundleError, "symlink"):
            scan_recipe_directory(self.root)

    def test_enforces_file_count_and_size_guards(self) -> None:
        (self.root / "one.txt").write_bytes(b"1234")
        (self.root / "two.txt").write_bytes(b"56")

        with self.assertRaisesRegex(RecipeBundleError, "exceeds 1 files"):
            scan_recipe_directory(self.root, max_files=1)
        with self.assertRaisesRegex(RecipeBundleError, "file exceeds 3 bytes"):
            scan_recipe_directory(self.root, max_file_size=3)
        with self.assertRaisesRegex(RecipeBundleError, "exceeds 5 total bytes"):
            scan_recipe_directory(self.root, max_total_size=5)

    def test_declared_assets_reject_path_escape(self) -> None:
        with self.assertRaisesRegex(RecipeBundleError, "unsafe recipe bundle path"):
            scan_declared_assets(self.root, ["../outside.txt"])

    def test_copied_bundle_and_manifest_verify(self) -> None:
        (self.root / "collection.toml").write_text("recipe_api = 1\n", encoding="utf-8")
        source = scan_recipe_directory(self.root)
        destination = self.root.parent / "captured"
        copy_bundle(source, destination)
        manifest_path = self.root.parent / "manifest.json"
        manifest_path.write_text(
            json.dumps(source.manifest(recipe_api=1, collection_id="example")),
            encoding="utf-8",
        )

        verified, manifest = verify_bundle_manifest(
            destination,
            manifest_path,
            expected_collection_id="example",
            expected_recipe_api=1,
        )

        self.assertEqual(verified.sha256, source.sha256)
        self.assertEqual(manifest["recipe_api"], 1)

        mismatched = source.manifest(recipe_api=1, collection_id="example")
        mismatched["bundle_sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(mismatched), encoding="utf-8")
        with self.assertRaisesRegex(RecipeBundleError, "mismatched canonical digest"):
            verify_bundle_manifest(destination, manifest_path)


if __name__ == "__main__":
    unittest.main()
