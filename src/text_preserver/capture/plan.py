"""Resolve collection selections into side-effect-free capture plans."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
from typing import Any, Sequence

from text_preserver.capture.engines.wget import WgetCommand, build_wget_command
from text_preserver.config import CollectionConfig, Config, SourceConfig


CAPTURE_ID_RE = re.compile(r"^([0-9]{8}T[0-9]{6}Z)-[a-z0-9]{6,16}$")
DRY_RUN_CAPTURE_ID = "<capture-id>"


class CapturePlanError(ValueError):
    """Raised when a requested capture selection cannot be planned."""


@dataclass(frozen=True)
class CapturePlan:
    collection_id: str
    capture_id: str
    capture_directory: Path
    capture_directory_must_not_exist: bool
    commands: tuple[WgetCommand, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "collection_id": self.collection_id,
            "capture_id": self.capture_id,
            "capture_directory": str(self.capture_directory),
            "capture_directory_must_not_exist": self.capture_directory_must_not_exist,
            "commands": [command.to_dict() for command in self.commands],
        }


def plan_capture(
    config: Config,
    collection_id: str,
    *,
    source_ids: Sequence[str] = (),
    capture_id: str | None = None,
) -> CapturePlan:
    """Create a deterministic plan without writing files or making requests."""
    collection = _find_collection(config, collection_id)
    selected_sources = _select_sources(collection, source_ids)
    resolved_capture_id = capture_id or DRY_RUN_CAPTURE_ID
    if capture_id is not None:
        _validate_capture_id(capture_id)

    capture_directory = (
        config.project.archive_root
        / "collections"
        / collection.id
        / "captures"
        / resolved_capture_id
    )
    if capture_directory.exists() or capture_directory.is_symlink():
        raise CapturePlanError(f"capture directory already exists: {capture_directory}")
    commands = tuple(
        build_wget_command(
            config.project,
            source,
            capture_directory,
        )
        for source in selected_sources
    )
    return CapturePlan(
        collection_id=collection.id,
        capture_id=resolved_capture_id,
        capture_directory=capture_directory,
        capture_directory_must_not_exist=True,
        commands=commands,
    )


def _validate_capture_id(capture_id: str) -> None:
    match = CAPTURE_ID_RE.fullmatch(capture_id)
    if match is None:
        raise CapturePlanError(
            "capture ID must be a UTC timestamp and 6-16 character suffix, "
            "for example 20260827T120000Z-a1b2c3"
        )
    try:
        datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ")
    except ValueError as exc:
        raise CapturePlanError(f"capture ID has an invalid UTC timestamp: {capture_id}") from exc


def _find_collection(config: Config, collection_id: str) -> CollectionConfig:
    for collection in config.collections:
        if collection.id == collection_id:
            return collection
    raise CapturePlanError(f"unknown collection: {collection_id}")


def _select_sources(
    collection: CollectionConfig,
    source_ids: Sequence[str],
) -> tuple[SourceConfig, ...]:
    if not source_ids:
        return collection.sources
    if len(source_ids) != len(set(source_ids)):
        raise CapturePlanError("source selections must not contain duplicates")
    requested = set(source_ids)
    known = {source.id for source in collection.sources}
    unknown = sorted(requested - known)
    if unknown:
        raise CapturePlanError(
            f"unknown source for collection {collection.id}: {unknown[0]}"
        )
    return tuple(source for source in collection.sources if source.id in requested)
