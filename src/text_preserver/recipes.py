"""Locate public collection recipes in source and installed distributions."""

from __future__ import annotations

from pathlib import Path
import re
import sysconfig


PUBLIC_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")


def _public_collection_roots() -> tuple[Path, ...]:
    module_path = Path(__file__).resolve()
    source_root = module_path.parents[2]
    if (
        module_path.parent.parent.name == "src"
        and (source_root / "pyproject.toml").is_file()
        and (source_root / "collections").is_dir()
    ):
        return (source_root / "collections",)

    data_roots: set[Path] = set()
    for scheme in sysconfig.get_scheme_names():
        data = sysconfig.get_path("data", scheme=scheme)
        if data is None:
            continue
        root = Path(data).resolve()
        if module_path.is_relative_to(root):
            data_roots.add(root)
    if data_roots:
        return tuple(
            root / "share/text-preserver/collections"
            for root in sorted(data_roots, key=lambda value: len(value.parts), reverse=True)
        )

    # `pip --target` places both the package and data-files under the target root.
    target_root = module_path.parent.parent
    return (target_root / "share/text-preserver/collections",)


def public_recipe_path(collection_id: str) -> Path:
    """Resolve a safe public collection ID to its recipe file."""
    if PUBLIC_ID_RE.fullmatch(collection_id) is None:
        raise ValueError(f"invalid public collection ID: {collection_id!r}")
    roots = _public_collection_roots()
    for root in roots:
        recipe = root / collection_id / "collection.toml"
        if recipe.is_file():
            return recipe
    return roots[-1] / collection_id / "collection.toml"
