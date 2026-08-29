from __future__ import annotations

import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from text_preserver.preservation.capture import plan_capture
from text_preserver.config import load_config
from text_preserver.recipes import public_recipe_path


REPOSITORY_ROOT = Path(__file__).parents[1]
RECIPE_ROOT = public_recipe_path("sacred-texts").parent
SPEC = importlib.util.spec_from_file_location(
    "sacred_texts_inventory",
    RECIPE_ROOT / "validator.py",
)
assert SPEC is not None and SPEC.loader is not None
inventory = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = inventory
SPEC.loader.exec_module(inventory)


class SacredTextsInventoryTests(unittest.TestCase):
    def test_counts_cdx_records_and_ignores_header(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.cdx.gz"
            with gzip.open(path, "wb") as stream:
                stream.write(b" CDX N b a m s k r M S V g\n")
                stream.write(b"com,example)/ 20200101000000 https://example.com/\n")
                stream.write(b"com,example)/a 20200101000001 https://example.com/a\n")

            self.assertEqual(inventory._count_cdx_records(path), 2)

    def test_validates_archive_fixture_but_reports_missing_publisher_media(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            capture = Path(directory) / "20260828T210000Z-a1b2c3"
            mirror = capture / "sources/internet-archive-2021/mirror/example"
            mirror.mkdir(parents=True)
            artifacts: dict[str, tuple[int | None, str | None]] = {}
            for name in inventory.EXPECTED_IA_ARTIFACTS:
                path = mirror / name
                if name == inventory.WARC_FILENAME:
                    with gzip.open(path, "wb") as stream:
                        stream.write(b"WARC/1.0\r\n")
                elif name == inventory.WARC_CDX_FILENAME:
                    with gzip.open(path, "wb") as stream:
                        stream.write(b" CDX N b a m s k r M S V g\nrecord-1\nrecord-2\n")
                else:
                    path.write_bytes(name.encode("ascii"))
                payload = path.read_bytes()
                artifacts[name] = (len(payload), hashlib.sha1(payload).hexdigest())
            metadata = {
                "metadata": {"identifier": inventory.ITEM_IDENTIFIER},
                "files": [
                    {"name": name, "size": str(size), "sha1": sha1}
                    for name, (size, sha1) in artifacts.items()
                ],
            }
            (mirror / inventory.METADATA_FILENAME).write_text(
                json.dumps(metadata),
                encoding="utf-8",
            )
            (capture / "capture.json").write_text(
                json.dumps(
                    {
                        "capture_id": capture.name,
                        "sources": [
                            {"source_id": "internet-archive-2021", "status": "complete"}
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch.object(inventory, "EXPECTED_IA_ARTIFACTS", artifacts),
                patch.object(inventory, "EXPECTED_CDX_RECORD_COUNT", 2),
            ):
                report = inventory.analyze_capture(
                    capture,
                    expected_work_count=0,
                    required_representation_kinds=("warc-record",),
                    required_source_ids=("internet-archive-2021",),
                )

            self.assertEqual(report["status"], "incomplete")
            self.assertEqual(
                report["errors"],
                [
                    "official ISTA DVD-ROM/USB 9.0 distribution is not preserved; "
                    "its published inventory contains 173566 files in 2884 directories"
                ],
            )
            self.assertEqual(report["artifact_count"], 8)
            self.assertEqual(report["cdx_record_count"], 2)
            self.assertEqual(report["expected_cdx_record_count"], 2)
            self.assertEqual(report["expected_work_count"], 0)
            self.assertEqual(report["publisher_media"]["preserved"], False)

    def test_public_recipe_builds_bounded_archive_plan(self) -> None:
        config = load_config(REPOSITORY_ROOT / "collections.example.toml")
        collection = next(item for item in config.collections if item.id == "sacred-texts")

        plan = plan_capture(
            config,
            "sacred-texts",
            source_ids=("internet-archive-2021",),
        )

        self.assertEqual(collection.analysis["expected_work_count"], 0)
        self.assertEqual(collection.analysis["prefer_preserved_adapter"], False)
        self.assertEqual(
            [source.id for source in collection.sources],
            ["internet-archive-2021", "wayback-download-recovery"],
        )
        self.assertEqual(len(plan.commands), 1)
        command = plan.commands[0]
        self.assertIn("--max-redirect=0", command.argv)
        self.assertNotIn("--recursive", command.argv)
        self.assertIn("--quota=2G", command.argv)
        documented = (RECIPE_ROOT / "seeds/internet-archive-2021.txt").read_text(
            encoding="utf-8"
        ).splitlines()
        self.assertEqual(documented, list(collection.sources[0].seeds))
        recovery_documented = (
            RECIPE_ROOT / "seeds/wayback-download-recovery.txt"
        ).read_text(encoding="utf-8").splitlines()
        self.assertEqual(recovery_documented, list(collection.sources[1].seeds))

    def test_validates_recovered_download_fixity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            capture = Path(directory)
            mirror = capture / "sources/wayback-download-recovery/mirror"
            mirror.mkdir(parents=True)
            expected = {}
            for name in inventory.RECOVERED_DOWNLOADS:
                path = mirror / name
                with gzip.GzipFile(filename=path, mode="wb", mtime=0) as stream:
                    stream.write(name.encode("ascii"))
                compressed = path.read_bytes()
                text = gzip.decompress(compressed)
                expected[name] = (
                    len(compressed),
                    hashlib.sha256(compressed).hexdigest(),
                    len(text),
                    hashlib.sha256(text).hexdigest(),
                )
            sources = {"wayback-download-recovery": {"status": "complete"}}

            with patch.object(inventory, "RECOVERED_DOWNLOADS", expected):
                reports, errors = inventory._validate_download_recovery(
                    capture,
                    sources,
                )

            self.assertEqual(errors, [])
            self.assertEqual(len(reports), 3)


if __name__ == "__main__":
    unittest.main()
