from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from text_preserver import recipes
from text_preserver.config import ConfigError, load_config
from text_preserver.recipes import public_recipe_path


VALID_CONFIG = """
[project]
archive_root = "./data/archive"
derived_root = "./data/derived"
workspace_root = "./data/workspace"
operator = "Test operator"
contact = "mailto:test@example.org"
user_agent = "text-preserver-test/1.0"

[defaults.capture]
wait = 2.0
quota = "50M"

[[collections]]
id = "test-collection"
title = "Test Collection"
enabled = true

[[collections.sources]]
id = "web"
kind = "web"
title = "Test website"
seeds = ["https://example.org/index.html"]
allowed_hosts = ["example.org"]

[[collections.sources]]
id = "dataset"
kind = "http-file"
title = "Canonical file"
required = false
seeds = ["https://data.example.org/corpus.xml"]
allowed_hosts = ["data.example.org"]

[collections.sources.capture]
recursive = false
page_requisites = false
convert_links = false
adjust_extension = false
""".strip()


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)

    def write_config(self, contents: str = VALID_CONFIG) -> Path:
        path = self.root / "collections.toml"
        path.write_text(contents, encoding="utf-8")
        return path

    def test_loads_and_resolves_configuration(self) -> None:
        config = load_config(self.write_config())

        self.assertEqual(config.project.archive_root, (self.root / "data/archive").resolve())
        self.assertEqual(config.project.derived_root, (self.root / "data/derived").resolve())
        self.assertEqual(len(config.collections), 1)
        collection = config.collections[0]
        self.assertEqual(collection.id, "test-collection")
        self.assertEqual(len(collection.sources), 2)
        self.assertEqual(collection.sources[0].capture["wait"], 2.0)
        self.assertTrue(collection.sources[0].capture["recursive"])
        self.assertFalse(collection.sources[1].capture["recursive"])

    def test_rejects_duplicate_collection_ids(self) -> None:
        duplicate = VALID_CONFIG + """

[[collections]]
id = "test-collection"
title = "Duplicate"

[[collections.sources]]
id = "web"
kind = "web"
title = "Web"
seeds = ["https://example.org/"]
allowed_hosts = ["example.org"]
"""
        with self.assertRaisesRegex(ConfigError, "duplicate collection ID"):
            load_config(self.write_config(duplicate))

    def test_rejects_seed_outside_allowed_hosts(self) -> None:
        invalid = VALID_CONFIG.replace(
            'seeds = ["https://example.org/index.html"]',
            'seeds = ["https://outside.example/index.html"]',
        )
        with self.assertRaisesRegex(ConfigError, "is not in allowed_hosts"):
            load_config(self.write_config(invalid))

    def test_rejects_credentials_in_seed(self) -> None:
        invalid = VALID_CONFIG.replace(
            "https://example.org/index.html",
            "https://user:secret@example.org/index.html",
        )
        with self.assertRaisesRegex(ConfigError, "credentials are not allowed"):
            load_config(self.write_config(invalid))

    def test_rejects_unmanaged_extra_arguments(self) -> None:
        invalid = VALID_CONFIG.replace(
            "quota = \"50M\"",
            'quota = "50M"\nextra_args = ["--header=Authorization: secret"]',
        )
        with self.assertRaisesRegex(ConfigError, "unknown key 'extra_args'"):
            load_config(self.write_config(invalid))

    def test_rejects_unknown_keys(self) -> None:
        invalid = VALID_CONFIG.replace(
            'user_agent = "text-preserver-test/1.0"',
            'user_agent = "text-preserver-test/1.0"\narchive_rooot = "typo"',
        )
        with self.assertRaisesRegex(ConfigError, "unknown key 'archive_rooot'"):
            load_config(self.write_config(invalid))

    def test_rejects_nested_data_roots(self) -> None:
        invalid = VALID_CONFIG.replace(
            'derived_root = "./data/derived"',
            'derived_root = "./data/archive/derived"',
        )
        with self.assertRaisesRegex(ConfigError, "separate, non-nested paths"):
            load_config(self.write_config(invalid))

    def test_rejects_non_finite_timing_values(self) -> None:
        for value in ("nan", "inf", "-inf"):
            with self.subTest(value=value):
                invalid = VALID_CONFIG.replace("wait = 2.0", f"wait = {value}")
                with self.assertRaisesRegex(ConfigError, "expected a non-negative number"):
                    load_config(self.write_config(invalid))

    def test_rejects_malformed_or_uppercase_hosts(self) -> None:
        for host in ("example..org", "-example.org", "EXAMPLE.org"):
            with self.subTest(host=host):
                invalid = VALID_CONFIG.replace(
                    'allowed_hosts = ["example.org"]',
                    f'allowed_hosts = ["{host}"]',
                    1,
                )
                with self.assertRaisesRegex(ConfigError, "expected a lowercase hostname"):
                    load_config(self.write_config(invalid))

    def test_resolved_configuration_lists_are_immutable(self) -> None:
        config = load_config(self.write_config())

        self.assertIsInstance(config.defaults_capture["success_exit_codes"], tuple)
        with self.assertRaises(TypeError):
            config.defaults_capture["wait"] = 0  # type: ignore[index]

    def test_example_configuration_loads(self) -> None:
        repository_root = Path(__file__).parents[1]
        config = load_config(repository_root / "collections.example.toml")

        self.assertEqual(config.collections[0].id, "example-corpus")
        self.assertEqual(config.collections[1].id, "etcsl")
        self.assertEqual(config.collections[1].recipe_path, public_recipe_path("etcsl"))

    def test_rejects_unsafe_public_recipe_id(self) -> None:
        config = 'recipes = ["public:../etcsl"]\n\n' + VALID_CONFIG.split(
            "[[collections]]", 1
        )[0]

        with self.assertRaisesRegex(ConfigError, "invalid public collection ID"):
            load_config(self.write_config(config))

    def test_public_recipe_lookup_skips_unrelated_collections_directory(self) -> None:
        unrelated = self.root / "python/collections"
        recipe_root = self.root / "share/text-preserver/collections"
        recipe = recipe_root / "etcsl/collection.toml"
        unrelated.mkdir(parents=True)
        recipe.parent.mkdir(parents=True)
        recipe.write_text("[collection]\n", encoding="utf-8")

        with patch.object(
            recipes,
            "_public_collection_roots",
            return_value=(unrelated, recipe_root),
        ):
            self.assertEqual(public_recipe_path("etcsl"), recipe)

    def test_loads_collection_recipe_relative_to_configuration(self) -> None:
        recipe = self.root / "recipes/example.toml"
        recipe.parent.mkdir()
        recipe.write_text(
            """
[collection]
id = "recipe-collection"
title = "Recipe Collection"

[[collection.sources]]
id = "web"
kind = "web"
title = "Website"
seeds = ["https://example.org/"]
allowed_hosts = ["example.org"]
""".strip(),
            encoding="utf-8",
        )
        operator_config = 'recipes = ["recipes/example.toml"]\n\n'
        operator_config += VALID_CONFIG.split("[[collections]]", 1)[0]

        config = load_config(self.write_config(operator_config))

        self.assertEqual([item.id for item in config.collections], ["recipe-collection"])
        self.assertEqual(config.collections[0].recipe_path, recipe.resolve())
        self.assertEqual(config.recipe_input_bytes[recipe.resolve()], recipe.read_bytes())

    def test_rejects_duplicate_ids_across_inline_and_recipe_collections(self) -> None:
        recipe = self.root / "recipe.toml"
        recipe.write_text(
            """
[collection]
id = "test-collection"
title = "Duplicate"

[[collection.sources]]
id = "web"
kind = "web"
title = "Website"
seeds = ["https://example.org/"]
allowed_hosts = ["example.org"]
""".strip(),
            encoding="utf-8",
        )
        config = 'recipes = ["recipe.toml"]\n\n' + VALID_CONFIG

        with self.assertRaisesRegex(ConfigError, "duplicate collection ID"):
            load_config(self.write_config(config))

    def test_rejects_missing_recipe(self) -> None:
        operator_config = 'recipes = ["missing.toml"]\n\n'
        operator_config += VALID_CONFIG.split("[[collections]]", 1)[0]

        with self.assertRaisesRegex(ConfigError, "collection recipe does not exist"):
            load_config(self.write_config(operator_config))

    def test_repository_schemas_are_valid_json(self) -> None:
        schema_root = Path(__file__).parents[1] / "schemas"
        for path in schema_root.glob("*.json"):
            with self.subTest(path=path.name):
                document = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(document["$schema"], "https://json-schema.org/draft/2020-12/schema")


if __name__ == "__main__":
    unittest.main()
