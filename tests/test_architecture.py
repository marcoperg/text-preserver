from __future__ import annotations

import ast
from pathlib import Path
import unittest


SOURCE_ROOT = Path(__file__).parents[1] / "src/text_preserver"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    module_parts = path.relative_to(SOURCE_ROOT.parent).with_suffix("").parts[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = module_parts[: len(module_parts) - node.level + 1]
                imports.add(".".join((*base, *(node.module or "").split("."))).rstrip("."))
            elif node.module is not None:
                imports.add(node.module)
    return imports


class ArchitectureTests(unittest.TestCase):
    def test_layer_dependencies(self) -> None:
        rules = {
            "preservation": (
                SOURCE_ROOT / "preservation",
                ("text_preserver.access", "text_preserver.research"),
            ),
            "access": (
                SOURCE_ROOT / "access",
                (
                    "text_preserver.research",
                    "text_preserver.preservation.capture.execute",
                ),
            ),
        }
        for layer, (root, forbidden) in rules.items():
            with self.subTest(layer=layer):
                imported = set().union(*(_imports(path) for path in root.rglob("*.py")))
                violations = sorted(
                    name
                    for name in imported
                    if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
                )
                self.assertEqual(violations, [])

    def test_neutral_modules_do_not_import_workflow_layers(self) -> None:
        rules = {
            "derived.py": (
                "text_preserver.preservation",
                "text_preserver.access",
                "text_preserver.research",
            ),
            "adapters.py": ("text_preserver.access", "text_preserver.research"),
        }
        for name, forbidden in rules.items():
            with self.subTest(module=name):
                violations = sorted(
                    imported
                    for imported in _imports(SOURCE_ROOT / name)
                    if any(
                        imported == prefix or imported.startswith(prefix + ".")
                        for prefix in forbidden
                    )
                )
                self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
