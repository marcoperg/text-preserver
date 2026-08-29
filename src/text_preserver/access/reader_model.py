"""Typed machine-readable access records emitted by static readers."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any, Mapping
import unicodedata
from urllib.parse import quote, urlsplit


ACCESS_MODEL_SCHEMA_VERSION = 2
SUPPORTED_ACCESS_MODEL_SCHEMA_VERSIONS = frozenset({1, 2})
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
MAX_ITEM_FACETS = 32
MAX_FACET_VALUES = 128


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
class AccessFacet:
    key: str
    label: str
    values: tuple[str, ...]
    artifact_ids: tuple[str, ...] = ()
    note: str | None = None


@dataclass(frozen=True)
class AccessItem:
    id: str
    label: str
    item_type: str
    route: str
    citation: str
    representations: tuple[AccessRepresentation, ...]
    rights: tuple[str, ...] = ()
    facets: tuple[AccessFacet, ...] = ()


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
    return _access_document(collection, ACCESS_MODEL_SCHEMA_VERSION)


def _access_document(
    collection: AccessCollection,
    schema_version: int,
) -> dict[str, Any]:
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
    facet_labels: dict[str, str] = {}
    facet_notes: dict[str, str | None] = {}
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
        if len(item.facets) > MAX_ITEM_FACETS:
            raise ValueError(f"access item exceeds {MAX_ITEM_FACETS} facets: {item.id}")
        facet_values: list[dict[str, Any]] = []
        facet_keys: set[str] = set()
        for facet in item.facets:
            if (
                not isinstance(facet.key, str)
                or _TYPE_RE.fullmatch(facet.key) is None
                or facet.key in facet_keys
                or not _valid_facet_text(facet.label)
                or not 1 <= len(facet.values) <= MAX_FACET_VALUES
                or any(not _valid_facet_text(value) for value in facet.values)
                or len(set(facet.values)) != len(facet.values)
                or not _string_tuple(facet.artifact_ids)
                or any(value not in artifact_ids for value in facet.artifact_ids)
                or facet.note is not None
                and not _valid_facet_text(facet.note)
            ):
                raise ValueError(f"invalid item facet: {item.id}")
            previous_label = facet_labels.setdefault(facet.key, facet.label)
            if previous_label != facet.label:
                raise ValueError(f"inconsistent item facet label: {facet.key}")
            if facet.key in facet_notes and facet_notes[facet.key] != facet.note:
                raise ValueError(f"inconsistent item facet note: {facet.key}")
            facet_notes[facet.key] = facet.note
            facet_keys.add(facet.key)
            facet_values.append(
                {
                    "key": facet.key,
                    "label": facet.label,
                    "values": list(facet.values),
                    "artifact_ids": list(facet.artifact_ids),
                    "note": facet.note,
                }
            )
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
        item_value: dict[str, Any] = {
                "id": item.id,
                "label": item.label,
                "type": item.item_type,
                "route": item.route,
                "citation": item.citation,
                "rights": list(item.rights),
                "representations": representation_values,
            }
        if schema_version >= 2:
            item_value["facets"] = facet_values
        item_values.append(item_value)

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
        "schema_version": schema_version,
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
    value = _access_json(collection, ACCESS_MODEL_SCHEMA_VERSION)
    if len(value.encode("utf-8")) > MAX_ACCESS_JSON_BYTES:
        raise ValueError(f"access graph JSON exceeds {MAX_ACCESS_JSON_BYTES} bytes")
    return value


def _access_json(collection: AccessCollection, schema_version: int) -> str:
    return json.dumps(
        _access_document(collection, schema_version), indent=2, sort_keys=True
    ) + "\n"


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


def load_access_collection(root: Path) -> AccessCollection:
    """Load and fully revalidate one canonical on-disk access graph."""
    path = root / "access.json"
    try:
        initial = path.lstat()
    except OSError as exc:
        raise ValueError("access.json is not a bounded regular file") from exc
    if (
        not stat.S_ISREG(initial.st_mode)
        or initial.st_nlink != 1
        or initial.st_size > MAX_ACCESS_JSON_BYTES
    ):
        raise ValueError("access.json is not a bounded regular file")
    try:
        with path.open("r", encoding="utf-8") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (initial.st_dev, initial.st_ino):
                raise ValueError("access.json changed while opening")
            source = stream.read()
        final = path.lstat()
        if (
            final.st_dev,
            final.st_ino,
            final.st_size,
            final.st_mtime_ns,
        ) != (
            initial.st_dev,
            initial.st_ino,
            initial.st_size,
            initial.st_mtime_ns,
        ):
            raise ValueError("access.json changed while reading")
        document = json.loads(source)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("access.json is not valid UTF-8 JSON") from exc
    collection = access_collection_from_document(document)
    schema_version = document.get("schema_version") if isinstance(document, dict) else None
    if type(schema_version) is not int or source != _access_json(
        collection, schema_version
    ):
        raise ValueError("access.json is not canonical for its declared schema")
    return collection


def access_collection_from_document(value: object) -> AccessCollection:
    """Rehydrate a serialized graph and require the complete schema contract."""
    document = _object_with_keys(
        value,
        {"schema_version", "collection", "items", "artifacts", "relations"},
        "access document",
    )
    schema_version = document["schema_version"]
    if (
        type(schema_version) is not int
        or schema_version not in SUPPORTED_ACCESS_MODEL_SCHEMA_VERSIONS
    ):
        raise ValueError("unsupported access model schema")
    collection_value = _object_with_keys(
        document["collection"],
        {"id", "label", "status", "route", "rights"},
        "access collection",
    )
    artifacts = tuple(_artifact_from_value(item) for item in _list(document["artifacts"], "artifacts"))
    items = tuple(
        _item_from_value(item, schema_version)
        for item in _list(document["items"], "items")
    )
    relations = tuple(
        _relation_from_value(item) for item in _list(document["relations"], "relations")
    )
    collection = AccessCollection(
        _string(collection_value["id"], "collection ID"),
        _string(collection_value["label"], "collection label"),
        _string(collection_value["status"], "collection status"),
        _string(collection_value["route"], "collection route"),
        items,
        artifacts,
        relations,
        _string_tuple_value(collection_value["rights"], "collection rights"),
    )
    if _access_document(collection, schema_version) != document:
        raise ValueError("access document does not match the canonical typed graph")
    return collection


def validate_access_indexes(root: Path) -> None:
    """Validate external segment indexes against their access graph references."""
    document_path = root / "access.json"
    if not document_path.exists():
        return
    load_access_collection(root)
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


def _valid_facet_text(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= MAX_ACCESS_TEXT_LENGTH
        and bool(value)
        and value == value.strip()
        and not any(unicodedata.category(character) == "Cc" for character in value)
    )


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


def _artifact_from_value(value: object) -> AccessArtifact:
    item = _object_with_optional_keys(
        value,
        {"id", "label", "role", "capture_path", "media_type"},
        {"sha256", "container_id", "member_path"},
        "artifact",
    )
    return AccessArtifact(
        _string(item["id"], "artifact ID"),
        _string(item["label"], "artifact label"),
        _string(item["role"], "artifact role"),
        _string(item["capture_path"], "artifact capture path"),
        _string(item["media_type"], "artifact media type"),
        _optional_string(item.get("sha256"), "artifact SHA-256"),
        _optional_string(item.get("container_id"), "artifact container ID"),
        _optional_string(item.get("member_path"), "artifact member path"),
    )


def _item_from_value(value: object, schema_version: int) -> AccessItem:
    fields = {"id", "label", "type", "route", "citation", "rights", "representations"}
    if schema_version >= 2:
        fields.add("facets")
    item = _object_with_keys(
        value,
        fields,
        "item",
    )
    return AccessItem(
        _string(item["id"], "item ID"),
        _string(item["label"], "item label"),
        _string(item["type"], "item type"),
        _string(item["route"], "item route"),
        _string(item["citation"], "item citation"),
        tuple(
            _representation_from_value(representation)
            for representation in _list(item["representations"], "representations")
        ),
        _string_tuple_value(item["rights"], "item rights"),
        (
            tuple(
                _facet_from_value(facet)
                for facet in _list(item["facets"], "item facets")
            )
            if schema_version >= 2
            else ()
        ),
    )


def _facet_from_value(value: object) -> AccessFacet:
    item = _object_with_keys(
        value, {"key", "label", "values", "artifact_ids", "note"}, "facet"
    )
    return AccessFacet(
        _string(item["key"], "facet key"),
        _string(item["label"], "facet label"),
        _string_tuple_value(item["values"], "facet values"),
        _string_tuple_value(item["artifact_ids"], "facet artifacts"),
        _optional_string(item["note"], "facet note"),
    )


def _representation_from_value(value: object) -> AccessRepresentation:
    item = _object_with_optional_keys(
        value,
        {"id", "label", "kind", "language", "route", "artifact_ids", "segments"},
        {"segment_index"},
        "representation",
    )
    return AccessRepresentation(
        _string(item["id"], "representation ID"),
        _string(item["label"], "representation label"),
        _string(item["kind"], "representation kind"),
        _string(item["language"], "representation language"),
        _string(item["route"], "representation route"),
        _string_tuple_value(item["artifact_ids"], "representation artifacts"),
        tuple(
            _segment_from_value(segment)
            for segment in _list(item["segments"], "segments")
        ),
        _optional_string(item.get("segment_index"), "segment index route"),
    )


def _segment_from_value(value: object) -> AccessSegment:
    item = _object_with_keys(value, {"id", "label", "route"}, "segment")
    return AccessSegment(
        _string(item["id"], "segment ID"),
        _string(item["label"], "segment label"),
        _string(item["route"], "segment route"),
    )


def _relation_from_value(value: object) -> AccessRelation:
    item = _object_with_keys(
        value,
        {"source_id", "relation", "target_id"},
        "relation",
    )
    return AccessRelation(
        _string(item["source_id"], "relation source"),
        _string(item["relation"], "relation type"),
        _string(item["target_id"], "relation target"),
    )


def _object_with_keys(value: object, keys: set[str], label: str) -> dict[str, Any]:
    return _object_with_optional_keys(value, keys, set(), label)


def _object_with_optional_keys(
    value: object,
    required: set[str],
    optional: set[str],
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"invalid {label}")
    if not required.issubset(value) or not set(value).issubset(required | optional):
        raise ValueError(f"invalid {label} fields")
    return value


def _list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"invalid {label}")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"invalid {label}")
    return value


def _optional_string(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _string(value, label)


def _string_tuple_value(value: object, label: str) -> tuple[str, ...]:
    return tuple(_string(item, label) for item in _list(value, label))
