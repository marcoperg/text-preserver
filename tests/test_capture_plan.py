from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from text_preserver.preservation.capture import CapturePlanError, plan_capture
from text_preserver.config import ConfigError, load_config

from tests.test_config import VALID_CONFIG


class CapturePlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.config_path = self.root / "collections.toml"
        self.config_path.write_text(VALID_CONFIG, encoding="utf-8")

    def test_web_plan_contains_reproducible_safety_and_warc_options(self) -> None:
        config = load_config(self.config_path)

        plan = plan_capture(config, "test-collection")
        command = plan.commands[0]

        self.assertEqual(plan.capture_id, "<capture-id>")
        self.assertEqual(command.argv[0], "wget")
        self.assertIn("--no-config", command.argv)
        self.assertIn("--no-netrc", command.argv)
        self.assertIn("--no-proxy", command.argv)
        self.assertIn("--max-redirect=0", command.argv)
        self.assertIn("--execute=robots=on", command.argv)
        self.assertIn("--domains=example.org", command.argv)
        self.assertIn("--recursive", command.argv)
        self.assertIn("--level=inf", command.argv)
        self.assertIn("--warc-cdx", command.argv)
        self.assertTrue(any(value.startswith("--warc-file=") for value in command.argv))
        self.assertIn("--warc-tempdir=warc/tmp", command.argv)
        self.assertTrue(any(value.startswith("--hsts-file=") for value in command.argv))
        self.assertIn("--output-file=logs/wget.log", command.argv)
        self.assertIn("--directory-prefix=mirror", command.argv)
        self.assertFalse(any(str(config.project.archive_root) in value for value in command.argv))
        self.assertTrue(command.required)
        self.assertEqual(command.success_exit_codes, (0,))
        self.assertEqual(command.argv[-1], "https://example.org/index.html")
        self.assertFalse(config.project.archive_root.exists())

    def test_direct_file_plan_omits_recursive_options(self) -> None:
        config = load_config(self.config_path)

        plan = plan_capture(
            config,
            "test-collection",
            source_ids=["dataset"],
            capture_id="20260827T120000Z-a1b2c3",
        )
        command = plan.commands[0]

        self.assertEqual(plan.capture_id, "20260827T120000Z-a1b2c3")
        self.assertEqual(command.source_id, "dataset")
        self.assertNotIn("--recursive", command.argv)
        self.assertNotIn("--page-requisites", command.argv)
        self.assertNotIn("--convert-links", command.argv)
        self.assertEqual(command.argv[-1], "https://data.example.org/corpus.xml")

    def test_plan_is_deterministic(self) -> None:
        config = load_config(self.config_path)

        first = plan_capture(config, "test-collection")
        second = plan_capture(config, "test-collection")

        self.assertEqual(first, second)

    def test_rejects_unknown_and_duplicate_sources(self) -> None:
        config = load_config(self.config_path)

        with self.assertRaisesRegex(CapturePlanError, "unknown source"):
            plan_capture(config, "test-collection", source_ids=["missing"])
        with self.assertRaisesRegex(CapturePlanError, "must not contain duplicates"):
            plan_capture(config, "test-collection", source_ids=["web", "web"])

    def test_rejects_invalid_capture_id(self) -> None:
        config = load_config(self.config_path)

        with self.assertRaisesRegex(CapturePlanError, "capture ID must be"):
            plan_capture(config, "test-collection", capture_id="unsafe/path")
        with self.assertRaisesRegex(CapturePlanError, "invalid UTC timestamp"):
            plan_capture(
                config,
                "test-collection",
                capture_id="20261399T996099Z-a1b2c3",
            )

    def test_rejects_existing_capture_directory(self) -> None:
        config = load_config(self.config_path)
        capture_id = "20260827T120000Z-a1b2c3"
        capture_directory = (
            config.project.archive_root
            / "collections"
            / "test-collection"
            / "captures"
            / capture_id
        )
        capture_directory.mkdir(parents=True)

        with self.assertRaisesRegex(CapturePlanError, "already exists"):
            plan_capture(config, "test-collection", capture_id=capture_id)

    def test_rejects_configuration_without_preservation_output(self) -> None:
        invalid = VALID_CONFIG.replace(
            'quota = "50M"',
            'quota = "50M"\nmirror = false\nwarc = false',
        )
        self.config_path.write_text(invalid, encoding="utf-8")

        with self.assertRaisesRegex(ConfigError, "mirror and warc cannot both be false"):
            load_config(self.config_path)

    def test_rejects_invalid_wget_byte_sizes(self) -> None:
        for key, value in (
            ("quota", "unlimited"),
            ("limit_rate", "-1K"),
            ("warc_max_size", "512K"),
        ):
            with self.subTest(key=key, value=value):
                if key == "quota":
                    invalid = VALID_CONFIG.replace('quota = "50M"', f'{key} = "{value}"')
                else:
                    invalid = VALID_CONFIG.replace(
                        'quota = "50M"',
                        f'quota = "50M"\n{key} = "{value}"',
                    )
                self.config_path.write_text(invalid, encoding="utf-8")
                with self.assertRaises(ConfigError):
                    load_config(self.config_path)


if __name__ == "__main__":
    unittest.main()
