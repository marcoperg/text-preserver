from __future__ import annotations

import json
from pathlib import Path
import tempfile
import tomllib
import unittest

from jsonschema import Draft202012Validator, FormatChecker, RefResolver

from text_preserver.config import ConfigError, load_config
from text_preserver.recipes import public_recipe_path

from tests.test_config import VALID_CONFIG


REPOSITORY_ROOT = Path(__file__).parents[1]


class ConfigurationSchemaConformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        schema = json.loads(
            (REPOSITORY_ROOT / "schemas/config.schema.json").read_text(encoding="utf-8")
        )
        cls.validator = Draft202012Validator(schema, format_checker=FormatChecker())

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)

    def assert_both_accept(self, document: str) -> None:
        raw = tomllib.loads(document)
        self.validator.validate(raw)
        path = self.root / "collections.toml"
        path.write_text(document, encoding="utf-8")
        load_config(path)

    def assert_both_reject(self, document: str) -> None:
        raw = tomllib.loads(document)
        self.assertTrue(list(self.validator.iter_errors(raw)))
        path = self.root / "collections.toml"
        path.write_text(document, encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_representative_valid_inline_configuration(self) -> None:
        self.assert_both_accept(VALID_CONFIG)

    def test_reviewed_redirect_structure_matches_runtime(self) -> None:
        valid = VALID_CONFIG.replace(
            'allowed_hosts = ["example.org"]',
            'allowed_hosts = ["example.org", "www.example.org"]\n'
            'reviewed_redirects = [{ from = "https://example.org/old", '
            'to = "https://www.example.org/new" }]',
            1,
        )
        self.assert_both_accept(valid)

        duplicate = valid.replace(
            'reviewed_redirects = [{ from = "https://example.org/old", '
            'to = "https://www.example.org/new" }]',
            'reviewed_redirects = ['
            '{ from = "https://example.org/old", to = "https://www.example.org/new" }, '
            '{ from = "https://example.org/old", to = "https://www.example.org/new" }]',
        )
        self.assert_both_reject(duplicate)

    def test_empty_recipes_are_valid_when_inline_collection_exists(self) -> None:
        self.assert_both_accept("recipes = []\n\n" + VALID_CONFIG)

    def test_empty_inline_collections_are_valid_when_recipe_exists(self) -> None:
        recipe = self.root / "recipe"
        recipe.mkdir()
        (recipe / "collection.toml").write_text(
            '''
recipe_api = 1

[collection]
id = "recipe-collection"
title = "Recipe Collection"

[[collection.sources]]
id = "web"
kind = "web"
title = "Website"
seeds = ["https://example.org/"]
allowed_hosts = ["example.org"]
'''.strip(),
            encoding="utf-8",
        )
        document = '''
recipes = ["recipe/collection.toml"]
collections = []

[project]
archive_root = "./archive"
operator = "Test operator"
contact = "mailto:test@example.org"
user_agent = "text-preserver-test/1.0"
'''.strip()
        self.assert_both_accept(document)

    def test_rejects_unknown_top_level_key(self) -> None:
        self.assert_both_reject(VALID_CONFIG + "\nunknown = true\n")

    def test_rejects_unsafe_collection_id(self) -> None:
        self.assert_both_reject(
            VALID_CONFIG.replace('id = "test-collection"', 'id = "Unsafe ID"', 1)
        )

    def test_rejects_missing_source_title(self) -> None:
        self.assert_both_reject(VALID_CONFIG.replace('title = "Test website"\n', "", 1))

    def test_rejects_invalid_analysis_type(self) -> None:
        invalid = VALID_CONFIG.replace(
            "enabled = true",
            'enabled = true\n\n[collections.analysis]\nprefer_preserved_adapter = "yes"',
        )
        self.assert_both_reject(invalid)

    def test_rejects_empty_configuration_scope(self) -> None:
        document = '''
recipes = []
collections = []

[project]
archive_root = "./archive"
operator = "Test operator"
contact = "mailto:test@example.org"
user_agent = "text-preserver-test/1.0"
'''.strip()
        self.assert_both_reject(document)

    def test_builtin_recipe_api_2_schema_matches_runtime(self) -> None:
        config_schema = json.loads(
            (REPOSITORY_ROOT / "schemas/config.schema.json").read_text(encoding="utf-8")
        )
        recipe_schema = json.loads(
            (REPOSITORY_ROOT / "schemas/collection-recipe.schema.json").read_text(
                encoding="utf-8"
            )
        )
        validator = Draft202012Validator(
            recipe_schema,
            resolver=RefResolver.from_schema(
                recipe_schema,
                store={config_schema["$id"]: config_schema},
            ),
            format_checker=FormatChecker(),
        )
        recipe_path = public_recipe_path("etcsl")
        raw = tomllib.loads(recipe_path.read_text(encoding="utf-8"))

        validator.validate(raw)
        invalid = dict(raw)
        invalid["collection"] = dict(raw["collection"])
        invalid["collection"]["analysis"] = dict(raw["collection"]["analysis"])
        invalid["collection"]["analysis"]["inventory_adapter"] = "validator.py"
        self.assertTrue(list(validator.iter_errors(invalid)))


if __name__ == "__main__":
    unittest.main()
