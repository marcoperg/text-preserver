"""Locate public collection recipes in source and installed distributions."""

from __future__ import annotations

from importlib import resources
from pathlib import Path
import re


PUBLIC_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")


def public_recipe_path(collection_id: str) -> Path:
    """Resolve a safe public collection ID to its recipe file."""
    if PUBLIC_ID_RE.fullmatch(collection_id) is None:
        raise ValueError(f"invalid public collection ID: {collection_id!r}")
    root = resources.files("text_preserver.builtin_recipes")
    return Path(str(root.joinpath(collection_id, "collection.toml")))
