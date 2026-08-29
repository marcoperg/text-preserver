"""Shared helpers for derived preservation and access outputs."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from text_preserver.config import CollectionConfig, Config


class AnalysisError(RuntimeError):
    """Raised when preservation analysis cannot run safely."""


def _find_collection(config: Config, collection_id: str) -> CollectionConfig:
    for collection in config.collections:
        if collection.id == collection_id:
            return collection
    raise AnalysisError(f"unknown collection: {collection_id}")


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        if path.is_symlink() or not path.is_file():
            raise AnalysisError(f"{label} is not a regular file: {path}")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AnalysisError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AnalysisError(f"{label} must contain a JSON object: {path}")
    return value


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ensure_derived_directory(root: Path, relative: Path) -> Path:
    resolved_root = root.resolve()
    if root.is_symlink():
        raise AnalysisError(f"derived root must not be a symlink: {root}")
    try:
        resolved_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AnalysisError(f"cannot create derived root {resolved_root}: {exc}") from exc
    current = resolved_root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise AnalysisError(f"derived output path must not contain symlinks: {current}")
        try:
            current.mkdir(exist_ok=True)
        except OSError as exc:
            raise AnalysisError(f"cannot create derived output directory {current}: {exc}") from exc
        if not current.is_dir():
            raise AnalysisError(f"derived output component is not a directory: {current}")
    if not current.resolve().is_relative_to(resolved_root):
        raise AnalysisError(f"derived output escapes configured root: {current}")
    return current
