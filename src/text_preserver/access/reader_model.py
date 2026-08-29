"""Typed machine-readable access records emitted by static readers."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any, Mapping
from urllib.parse import quote, urlsplit


ACCESS_MODEL_SCHEMA_VERSION = 1
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_TYPE_RE = re.compile(r"[a-z][a-z0-9_-]*")
_ACCESS_ID_RE = re.compile(
    r"tp:[A-Za-z0-9._~%'-]+/[a-z][a-z0-9_-]*(?:/[A-Za-z0-9._~%'-]+)?"
)
_STATUS_VALUES = {"complete", "complete_with_warnings", "incomplete"}
MAX_ACCESS_ITEMS = 2_000
MAX_ACCESS_ARTIFACTS = 4_000
MAX_ACCESS_SEGMENTS = 100_000
MAX_ACCESS_JSON_BYTES = 64 * 1024 * 1024
MAX_ACCESS_ID_LENGTH = 2_048
MAX_ACCESS_TEXT_LENGTH = 16_384
MAX_EXTERNAL_ACCESS_SEGMENTS = 500_000


@dataclass(frozen=True)
class AccessArtifact:
    id: str
    label: str
    role: str
    capture_path: str
    media_type: str
    sha256: str | None = None
    container_id: str | None = None
    member_path: str | None = None


@dataclass(frozen=True)
class AccessSegment:
    id: str
    label: str
    route: str


@dataclass(frozen=True)
class AccessRepresentation:
    id: str
    label: str
    kind: str
    language: str
    route: str
    artifact_ids: tuple[str, ...]
    segments: tuple[AccessSegment, ...] = ()
    segment_index: str | None = None


@dataclass(frozen=True)
class AccessItem:
    id: str
    label: str
    item_type: str
    route: str
    citation: str
    representations: tuple[AccessRepresentation, ...]
    rights: tuple[str, ...] = ()


@dataclass(frozen=True)
class AccessRelation:
    source_id: str
    relation: str
    target_id: str


@dataclass(frozen=True)
class AccessCollection:
    id: str
    label: str
    status: str
    route: str
    items: tuple[AccessItem, ...]
    artifacts: tuple[AccessArtifact, ...]
    relations: tuple[AccessRelation, ...] = ()
    rights: tuple[str, ...] = ()


def access_id(collection_id: str, kind: str, value: str) -> str:
    """Create a stable, portable identifier without imposing a text hierarchy."""
    if _TYPE_RE.fullmatch(kind) is None:
        raise ValueError(f"invalid access identifier kind: {kind!r}")
    return "tp:{}/{}{}".format(
        quote(collection_id, safe="-._~"),
        kind,
        f"/{quote(value, safe='-._~')}" if value else "",
    )


def route_token(value: str) -> str:
    """Encode source identifiers without browser percent-decoding ambiguity."""
    safe = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._'")
    return "".join(
        character
        if character in safe
        else "".join(f"~{byte:02x}" for byte in character.encode("utf-8"))
        for character in value
    )


def access_document(collection: AccessCollection) -> dict[str, Any]:
    """Validate and serialize one collection's common access graph."""
    if (
        not _valid_text(collection.label)
        or not isinstance(collection.status, str)
        or collection.status not in _STATUS_VALUES
        or not _string_tuple(collection.rights)
    ):
        raise ValueError(f"invalid access collection status: {collection.status!r}")
    if len(collection.items) > MAX_ACCESS_ITEMS:
        raise ValueError(f"access graph exceeds {MAX_ACCESS_ITEMS} items")
    if len(collection.artifacts) > MAX_ACCESS_ARTIFACTS:
        raise ValueError(f"access graph exceeds {MAX_ACCESS_ARTIFACTS} artifacts")
    _validate_route(collection.route, "collection route")
    identifiers: set[str] = set()
    endpoint_ids: set[str] = set()
    artifact_ids: set[str] = set()

    def register(identifier: str, label: str) -> None:
        if (
            not _valid_access_identifier(identifier)
            or identifier in identifiers
        ):
            raise ValueError(f"invalid or duplicate {label} ID: {identifier!r}")
        identifiers.add(identifier)

    register(collection.id, "collection")
    endpoint_ids.add(collection.id)
    artifact_values: list[dict[str, Any]] = []
    for artifact in collection.artifacts:
        register(artifact.id, "artifact")
        artifact_ids.add(artifact.id)
        _validate_capture_path(artifact.capture_path)
        if (
            not _valid_text(artifact.label)
            or not isinstance(artifact.media_type, str)
            or not artifact.media_type
            or len(artifact.media_type) > MAX_ACCESS_TEXT_LENGTH
            or not isinstance(artifact.role, str)
            or _TYPE_RE.fullmatch(artifact.role) is None
        ):
            raise ValueError(f"invalid artifact role: {artifact.role!r}")
        if artifact.sha256 is not None and (
            not isinstance(artifact.sha256, str)
            or _DIGEST_RE.fullmatch(artifact.sha256) is None
        ):
            raise ValueError(f"invalid artifact SHA-256: {artifact.id}")
        value: dict[str, Any] = {
            "id": artifact.id,
            "label": artifact.label,
            "role": artifact.role,
            "capture_path": artifact.capture_path,
            "media_type": artifact.media_type,
        }
        for key in ("sha256", "container_id", "member_path"):
            item = getattr(artifact, key)
            if item is not None:
                if not isinstance(item, str):
                    raise ValueError(f"invalid artifact {key}: {artifact.id}")
                if key == "member_path":
                    _validate_capture_path(item)
                value[key] = item
        artifact_values.append(value)
    for artifact in collection.artifacts:
        if artifact.container_id is not None and (
            artifact.container_id == artifact.id or artifact.container_id not in artifact_ids
        ):
            raise ValueError(f"invalid artifact container: {artifact.id}")
        seen_containers = {artifact.id}
        container_id = artifact.container_id
        while container_id is not None:
            if container_id in seen_containers:
                raise ValueError(f"artifact container cycle: {artifact.id}")
            seen_containers.add(container_id)
            container = next(value for value in collection.artifacts if value.id == container_id)
            container_id = container.container_id

    item_values: list[dict[str, Any]] = []
    item_routes: set[str] = set()
    representation_routes: set[str] = set()
    segment_routes: set[str] = set()
    segment_count = 0
    for item in collection.items:
        register(item.id, "item")
        endpoint_ids.add(item.id)
        _validate_route(item.route, "item route")
        if item.route in item_routes:
            raise ValueError(f"duplicate item route: {item.route!r}")
        item_routes.add(item.route)
        if (
            not _valid_text(item.label)
            or not _valid_text(item.citation)
            or not _string_tuple(item.rights)
            or not isinstance(item.item_type, str)
            or _TYPE_RE.fullmatch(item.item_type) is None
        ):
            raise ValueError(f"invalid item type: {item.item_type!r}")
        representation_values: list[dict[str, Any]] = []
        for representation in item.representations:
            register(representation.id, "representation")
            endpoint_ids.add(representation.id)
            _validate_route(representation.route, "representation route")
            if representation.route in representation_routes:
                raise ValueError(f"duplicate representation route: {representation.route!r}")
            representation_routes.add(representation.route)
            if (
                not _valid_text(representation.label)
                or not _valid_text(representation.language)
                or not isinstance(representation.kind, str)
                or _TYPE_RE.fullmatch(representation.kind) is None
                or not _string_tuple(representation.artifact_ids)
            ):
                raise ValueError(f"invalid representation kind: {representation.kind!r}")
            if any(value not in artifact_ids for value in representation.artifact_ids):
                raise ValueError(f"representation references an unknown artifact: {representation.id}")
            if representation.segment_index is not None:
                _validate_route(representation.segment_index, "segment index route")
                if representation.segments:
                    raise ValueError(
                        f"representation has inline and indexed segments: {representation.id}"
                    )
            segment_values: list[dict[str, str]] = []
            for segment in representation.segments:
                segment_count += 1
                if segment_count > MAX_ACCESS_SEGMENTS:
                    raise ValueError(f"access graph exceeds {MAX_ACCESS_SEGMENTS} segments")
                register(segment.id, "segment")
                endpoint_ids.add(segment.id)
                _validate_route(segment.route, "segment route")
                if not _valid_text(segment.label):
                    raise ValueError(f"invalid segment label: {segment.id}")
                if segment.route in segment_routes:
                    raise ValueError(f"duplicate segment route: {segment.route!r}")
                segment_routes.add(segment.route)
                segment_values.append(
                    {"id": segment.id, "label": segment.label, "route": segment.route}
                )
            representation_value: dict[str, Any] = {
                "id": representation.id,
                "label": representation.label,
                "kind": representation.kind,
                "language": representation.language,
                "route": representation.route,
                "artifact_ids": list(representation.artifact_ids),
                "segments": segment_values,
            }
            if representation.segment_index is not None:
                representation_value["segment_index"] = representation.segment_index
            representation_values.append(representation_value)
        item_values.append(
            {
                "id": item.id,
                "label": item.label,
                "type": item.item_type,
                "route": item.route,
                "citation": item.citation,
                "rights": list(item.rights),
                "representations": representation_values,
            }
        )

    relation_values: list[dict[str, str]] = []
    for relation in collection.relations:
        if (
            not isinstance(relation.source_id, str)
            or not isinstance(relation.target_id, str)
            or not isinstance(relation.relation, str)
            or relation.source_id not in endpoint_ids
            or relation.target_id not in endpoint_ids
            or _TYPE_RE.fullmatch(relation.relation) is None
        ):
            raise ValueError(f"invalid access relation: {relation!r}")
        relation_values.append(
            {
                "source_id": relation.source_id,
                "relation": relation.relation,
                "target_id": relation.target_id,
            }
        )
    return {
        "schema_version": ACCESS_MODEL_SCHEMA_VERSION,
        "collection": {
            "id": collection.id,
            "label": collection.label,
            "status": collection.status,
            "route": collection.route,
            "rights": list(collection.rights),
        },
        "items": item_values,
        "artifacts": artifact_values,
        "relations": relation_values,
    }


def access_json(collection: AccessCollection) -> str:
    """Serialize a validated access graph as deterministic UTF-8 JSON text."""
    value = json.dumps(access_document(collection), indent=2, sort_keys=True) + "\n"
    if len(value.encode("utf-8")) > MAX_ACCESS_JSON_BYTES:
        raise ValueError(f"access graph JSON exceeds {MAX_ACCESS_JSON_BYTES} bytes")
    return value


def access_segment_json(representation_id: str, segment: AccessSegment) -> str:
    """Serialize one validated external segment-index record as compact JSON Lines."""
    if not _valid_access_identifier(representation_id):
        raise ValueError(f"invalid representation ID: {representation_id!r}")
    if not _valid_access_identifier(segment.id) or not _valid_text(segment.label):
        raise ValueError(f"invalid segment record: {segment.id!r}")
    _validate_route(segment.route, "segment route")
    return json.dumps(
        {
            "representation_id": representation_id,
            "segment": {
                "id": segment.id,
                "label": segment.label,
                "route": segment.route,
            },
        },
        separators=(",", ":"),
        sort_keys=True,
    ) + "\n"


def validate_access_indexes(root: Path) -> None:
    """Validate external segment indexes against their access graph references."""
    document_path = root / "access.json"
    if not document_path.exists():
        return
    if document_path.is_symlink() or document_path.stat().st_size > MAX_ACCESS_JSON_BYTES:
        raise ValueError("access.json is not a bounded regular file")
    try:
        document = json.loads(document_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("access.json is not valid UTF-8 JSON") from exc
    if (
        not isinstance(document, dict)
        or not isinstance(document.get("collection"), dict)
        or not isinstance(document.get("artifacts"), list)
        or not isinstance(document.get("items"), list)
    ):
        raise ValueError("access.json has no typed item graph")

    indexed_representations: dict[str, set[str]] = {}
    graph_ids: set[str] = set()
    segment_routes: set[str] = set()
    _register_graph_identifier(document["collection"].get("id"), graph_ids)
    for artifact in document["artifacts"]:
        if not isinstance(artifact, dict):
            raise ValueError("access.json contains an invalid artifact")
        _register_graph_identifier(artifact.get("id"), graph_ids)
    for item in document["items"]:
        if not isinstance(item, dict) or not isinstance(item.get("representations"), list):
            raise ValueError("access.json contains an invalid item")
        _register_graph_identifier(item.get("id"), graph_ids)
        for representation in item["representations"]:
            if not isinstance(representation, dict):
                raise ValueError("access.json contains an invalid representation")
            representation_id = representation.get("id")
            if not isinstance(representation_id, str) or not _valid_access_identifier(
                representation_id
            ):
                raise ValueError("access.json contains an invalid representation ID")
            _register_graph_identifier(representation_id, graph_ids)
            index_route = representation.get("segment_index")
            if index_route is not None:
                _validate_route(index_route, "segment index route")
                if "#" in index_route:
                    raise ValueError("segment index route must identify a file")
                indexed_representations.setdefault(index_route, set()).add(representation_id)
            inline_segments = representation.get("segments")
            if not isinstance(inline_segments, list):
                raise ValueError("access.json contains invalid inline segments")
            for segment in inline_segments:
                _register_serialized_segment(segment, graph_ids, segment_routes)

    external_count = 0
    for index_route, representation_ids in indexed_representations.items():
        index_path = root / index_route
        if (
            index_path.is_symlink()
            or not index_path.is_file()
            or index_path.stat().st_size > 128 * 1024 * 1024
        ):
            raise ValueError(f"segment index is not a bounded regular file: {index_route}")
        try:
            with index_path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    external_count += 1
                    if external_count > MAX_EXTERNAL_ACCESS_SEGMENTS:
                        raise ValueError(
                            f"access graph exceeds {MAX_EXTERNAL_ACCESS_SEGMENTS} external segments"
                        )
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise ValueError(f"invalid segment index JSON: {index_route}") from exc
                    if not isinstance(record, dict) or set(record) != {
                        "representation_id",
                        "segment",
                    }:
                        raise ValueError(f"invalid segment index record: {index_route}")
                    representation_id = record["representation_id"]
                    if representation_id not in representation_ids:
                        raise ValueError(
                            f"segment index references an unknown representation: {index_route}"
                        )
                    segment = _register_serialized_segment(
                        record["segment"], graph_ids, segment_routes
                    )
                    if line != access_segment_json(representation_id, segment):
                        raise ValueError(f"noncanonical segment index record: {index_route}")
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"cannot read segment index: {index_route}") from exc


def reader_model_identity() -> Mapping[str, object]:
    source = Path(__file__).read_bytes()
    return {
        "schema_version": ACCESS_MODEL_SCHEMA_VERSION,
        "sha256": hashlib.sha256(source).hexdigest(),
    }


def _validate_route(value: str, label: str) -> None:
    if not isinstance(value, str) or len(value) > MAX_ACCESS_TEXT_LENGTH:
        raise ValueError(f"invalid {label}: {value!r}")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ValueError(f"invalid {label}: {value!r}") from exc
    path_value = value.split("#", 1)[0]
    path = PurePosixPath(path_value)
    if (
        not path_value
        or "\\" in value
        or parsed.scheme
        or parsed.netloc
        or parsed.query
        or "%" in path_value
        or path.is_absolute()
        or ".." in path.parts
        or path.as_posix() != path_value
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"invalid {label}: {value!r}")


def _validate_capture_path(value: str) -> None:
    if not isinstance(value, str) or len(value) > MAX_ACCESS_TEXT_LENGTH:
        raise ValueError(f"invalid artifact capture path: {value!r}")
    path = PurePosixPath(value)
    if (
        not value
        or value == "."
        or "\\" in value
        or path.is_absolute()
        or ".." in path.parts
        or path.as_posix() != value
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"invalid artifact capture path: {value!r}")


def _string_tuple(value: object) -> bool:
    return type(value) is tuple and all(_valid_text(item) for item in value)


def _valid_text(value: object) -> bool:
    return isinstance(value, str) and len(value) <= MAX_ACCESS_TEXT_LENGTH


def _valid_access_identifier(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= MAX_ACCESS_ID_LENGTH
        and _ACCESS_ID_RE.fullmatch(value) is not None
        and re.search(r"%(?![0-9A-F]{2})", value) is None
    )


def _register_serialized_segment(
    value: object,
    identifiers: set[str],
    routes: set[str],
) -> AccessSegment:
    if not isinstance(value, dict) or set(value) != {"id", "label", "route"}:
        raise ValueError("invalid serialized access segment")
    segment = AccessSegment(value["id"], value["label"], value["route"])
    if not _valid_access_identifier(segment.id) or not _valid_text(segment.label):
        raise ValueError(f"invalid serialized access segment: {segment.id!r}")
    _validate_route(segment.route, "segment route")
    if segment.id in identifiers or segment.route in routes:
        raise ValueError(f"duplicate serialized access segment: {segment.id}")
    identifiers.add(segment.id)
    routes.add(segment.route)
    return segment


def _register_graph_identifier(value: object, identifiers: set[str]) -> None:
    if not _valid_access_identifier(value) or value in identifiers:
        raise ValueError(f"invalid or duplicate access graph ID: {value!r}")
    assert isinstance(value, str)
    identifiers.add(value)
