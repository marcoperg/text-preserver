"""Build and query an immutable cross-collection access catalogue."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
from html import escape as escape_html
from html.parser import HTMLParser
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import shutil
import sqlite3
import stat
import sys
import tempfile
from typing import Any, Iterator, Mapping, Sequence
import unicodedata

from text_preserver.access.reader import (
    _atomic_write_text,
    _canonical_json,
    _fsync_tree,
    _make_tree_read_only,
    _make_tree_writable,
    current_reader_generation,
)
from text_preserver.access.reader_model import (
    AccessCollection,
    AccessRepresentation,
    access_id,
    load_access_collection,
    reader_model_identity,
    validate_access_indexes,
)
from text_preserver.access.reader_shell import (
    ReaderLink,
    reader_shell_identity,
    reader_stylesheet,
    render_document,
    render_navigation,
    render_notice,
    render_status,
)
from text_preserver.config import CollectionConfig, Config
from text_preserver.derived import (
    AnalysisError,
    _ensure_derived_directory,
    _find_collection,
    _fsync_directory,
    _read_json,
    _sha256,
)


CATALOGUE_SCHEMA_VERSION = 1
CATALOGUE_APPLICATION_ID = 0x54504654
MAX_CATALOGUE_FILES = 16
MAX_CATALOGUE_BYTES = 512 * 1024 * 1024
MAX_HTML_BYTES = 128 * 1024 * 1024
MAX_SEARCH_TEXT_BYTES = 256 * 1024 * 1024
MAX_SEARCH_QUERY_LENGTH = 1_024
MAX_SEARCH_LIMIT = 100
SUCCESS_STATUSES = {"complete", "complete_with_warnings"}
BLOCK_TAGS = frozenset({
    "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt", "figcaption",
    "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "li", "main",
    "ol", "p", "pre", "section", "table", "td", "th", "tr", "ul",
})
EXCLUDED_TAGS = frozenset({"script", "style", "template", "noscript"})
EXCLUDED_CLASSES = frozenset({"reader-artifact", "reader-citation"})
VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
    "param", "source", "track", "wbr",
})


@dataclass(frozen=True)
class CatalogueResult:
    output_directory: Path
    index_path: Path
    database_path: Path
    metadata_path: Path
    metadata: Mapping[str, Any]
    current_index_path: Path | None
    current_catalogue_updated: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "output_directory": str(self.output_directory),
            "index_path": str(self.index_path),
            "database_path": str(self.database_path),
            "metadata_path": str(self.metadata_path),
            "current_index_path": (
                str(self.current_index_path) if self.current_index_path is not None else None
            ),
            "current_catalogue_updated": self.current_catalogue_updated,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class SearchHit:
    collection_id: str
    item_id: str
    representation_id: str
    title: str
    representation_label: str
    language: str
    kind: str
    rank: float
    snippet: str
    path: Path
    fragment: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "collection_id": self.collection_id,
            "item_id": self.item_id,
            "representation_id": self.representation_id,
            "title": self.title,
            "representation_label": self.representation_label,
            "language": self.language,
            "kind": self.kind,
            "rank": self.rank,
            "snippet": self.snippet,
            "path": str(self.path),
            "fragment": self.fragment,
            "uri": self.path.resolve().as_uri()
            + (f"#{self.fragment}" if self.fragment else ""),
        }


@dataclass(frozen=True)
class SearchResult:
    query: str
    database_path: Path
    hits: tuple[SearchHit, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "database_path": str(self.database_path),
            "hits": [hit.to_dict() for hit in self.hits],
        }


@dataclass(frozen=True)
class _ReaderInput:
    configured: CollectionConfig
    root: Path
    relative_root: str
    metadata: Mapping[str, Any]
    access: AccessCollection
    access_sha256: str


def build_catalogue(
    config: Config,
    collection_ids: Sequence[str] | None = None,
) -> CatalogueResult:
    """Build one deterministic catalogue and FTS5 index from current readers."""
    selected = _selected_collections(config, collection_ids)
    reader_inputs: list[_ReaderInput] = []
    unavailable: list[dict[str, object]] = []
    warnings: list[str] = []
    for collection in selected:
        loaded = _load_reader_input(config, collection)
        if isinstance(loaded, str):
            unavailable.append(_unavailable_collection(collection, loaded))
            warnings.append(f"{collection.id}: {loaded}")
        else:
            reader_inputs.append(loaded)

    sqlite_identity = _sqlite_identity()
    build_inputs = {
        "schema_version": CATALOGUE_SCHEMA_VERSION,
        "selection": [collection.id for collection in selected],
        "readers": [
            {
                "collection_id": value.configured.id,
                "capture_id": value.metadata["capture_id"],
                "build_key": value.metadata["build_key"],
                "output_tree_sha256": value.metadata["output_tree"]["sha256"],
                "access_sha256": value.access_sha256,
            }
            for value in reader_inputs
        ],
        "unavailable": unavailable,
        "renderer": _catalogue_identity(),
        "reader_model": dict(reader_model_identity()),
        "reader_shell": dict(reader_shell_identity()),
        "sqlite": sqlite_identity,
        "python": {
            "implementation": sys.implementation.name,
            "version": list(sys.version_info[:3]),
            "unicode_version": unicodedata.unidata_version,
        },
        "fts": {
            "tokenizer": "unicode61 remove_diacritics 0",
            "document_granularity": "representation",
            "normalization_version": 1,
        },
    }
    build_key = hashlib.sha256(_canonical_json(build_inputs)).hexdigest()
    status = (
        "complete"
        if not unavailable
        else "complete_with_warnings"
        if reader_inputs
        else "incomplete"
    )
    derived_root = _ensure_derived_directory(config.project.derived_root, Path("."))
    generations = _ensure_derived_directory(derived_root, Path("catalogue-generations"))
    staging = Path(tempfile.mkdtemp(dir=generations, prefix=".build-"))
    staging_cleanup: Path | None = staging
    try:
        projection = _catalogue_projection(reader_inputs, unavailable)
        (staging / "assets").mkdir()
        (staging / "assets/reader.css").write_text(reader_stylesheet(), encoding="utf-8")
        (staging / "assets/catalogue.css").write_text(
            _catalogue_stylesheet(), encoding="utf-8"
        )
        (staging / "catalogue.json").write_text(
            json.dumps(projection, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (staging / "index.html").write_text(
            _render_catalogue(projection, status, warnings), encoding="utf-8"
        )
        database_path = staging / "catalogue.sqlite"
        logical_sha256, document_count = _build_database(
            database_path,
            reader_inputs,
            build_key,
        )
        _validate_database(database_path, verify_external_content=True)
        database_sha256 = _sha256(database_path)
        output_tree = _validate_catalogue_tree(staging)
        summary = {
            "collection_count": len(selected),
            "available_collection_count": len(reader_inputs),
            "unavailable_collection_count": len(unavailable),
            "item_count": sum(len(value.access.items) for value in reader_inputs),
            "representation_count": document_count,
            "document_count": document_count,
        }
        metadata: dict[str, Any] = {
            "schema_version": CATALOGUE_SCHEMA_VERSION,
            "status": status,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "build_key": build_key,
            "build_inputs": build_inputs,
            "output_tree": output_tree,
            "database": {
                "path": "catalogue.sqlite",
                "sha256": database_sha256,
                "logical_sha256": logical_sha256,
                "bytes": database_path.stat().st_size,
            },
            "summary": summary,
            "warnings": warnings,
        }
        with _catalogue_lock(derived_root):
            generation, metadata = _publish_catalogue(
                derived_root,
                staging,
                metadata,
            )
            staging_cleanup = None
            updated = status in SUCCESS_STATUSES
            current: Path | None
            if updated:
                _atomic_write_text(
                    derived_root / "LATEST-CATALOGUE",
                    f"catalogue-generations/{build_key}\n",
                )
                _fsync_directory(derived_root)
                current = generation / "index.html"
            else:
                current = _existing_catalogue_index(derived_root)
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        raise AnalysisError(f"cannot build access catalogue: {exc}") from exc
    finally:
        if staging_cleanup is not None and staging_cleanup.exists():
            _make_tree_writable(staging_cleanup)
            shutil.rmtree(staging_cleanup)
    return CatalogueResult(
        generation,
        generation / "index.html",
        generation / "catalogue.sqlite",
        generation / "metadata.json",
        metadata,
        current,
        updated,
    )


def current_catalogue_index(config: Config) -> Path:
    """Return the validated current catalogue index."""
    value = _existing_catalogue_index(config.project.derived_root)
    if value is None:
        raise AnalysisError("no current access catalogue")
    return value


def search_catalogue(
    config: Config,
    query: str,
    *,
    collection_ids: Sequence[str] = (),
    languages: Sequence[str] = (),
    kinds: Sequence[str] = (),
    item_types: Sequence[str] = (),
    limit: int = 20,
) -> SearchResult:
    """Query the current immutable FTS5 index and return exact reader paths."""
    query = unicodedata.normalize("NFC", query)
    if not query or len(query) > MAX_SEARCH_QUERY_LENGTH or any(ord(char) < 32 for char in query):
        raise AnalysisError("search query is empty, too long, or contains control characters")
    if type(limit) is not int or not 1 <= limit <= MAX_SEARCH_LIMIT:
        raise AnalysisError(f"search limit must be between 1 and {MAX_SEARCH_LIMIT}")
    index = current_catalogue_index(config)
    generation = index.parent
    metadata = _read_json(generation / "metadata.json", "catalogue metadata")
    database = generation / "catalogue.sqlite"
    if _sha256(database) != _mapping(metadata.get("database")).get("sha256"):
        raise AnalysisError("catalogue database digest does not match metadata")
    clauses = ["document_fts MATCH ?"]
    parameters: list[object] = [query]
    for column, values in (
        ("d.collection_id", collection_ids),
        ("d.language", languages),
        ("d.kind", kinds),
        ("d.item_type", item_types),
    ):
        if values:
            clauses.append(f"{column} IN ({','.join('?' for _ in values)})")
            parameters.extend(values)
    parameters.append(limit)
    sql = f"""
SELECT d.collection_id,d.item_id,d.representation_id,d.title,
       d.representation_label,d.language,d.kind,d.reader_root,d.route,
       bm25(document_fts,8.0,3.0,1.0) AS rank,
       snippet(document_fts,2,'[',']',' ... ',24) AS snippet
FROM document_fts JOIN documents AS d ON d.docid=document_fts.rowid
WHERE {' AND '.join(clauses)}
ORDER BY rank,d.collection_id COLLATE BINARY,d.representation_id COLLATE BINARY
LIMIT ?
"""
    try:
        connection = sqlite3.connect(_sqlite_readonly_uri(database), uri=True)
        disable_extensions = getattr(connection, "enable_load_extension", None)
        if disable_extensions is not None:
            disable_extensions(False)
        rows = connection.execute(sql, parameters).fetchall()
    except sqlite3.Error as exc:
        raise AnalysisError(f"cannot query catalogue: {exc}") from exc
    finally:
        if "connection" in locals():
            connection.close()
    hits: list[SearchHit] = []
    for row in rows:
        path_value, fragment = _split_route(row[8])
        reader_root = _safe_relative_directory(config.project.derived_root, row[7])
        path = _safe_relative_file(reader_root, path_value)
        hits.append(
            SearchHit(
                row[0], row[1], row[2], row[3], row[4], row[5], row[6],
                float(row[9]), row[10], path, fragment,
            )
        )
    return SearchResult(query, database, tuple(hits))


def _selected_collections(
    config: Config,
    collection_ids: Sequence[str] | None,
) -> tuple[CollectionConfig, ...]:
    if collection_ids:
        if len(collection_ids) != len(set(collection_ids)):
            raise AnalysisError("catalogue collection IDs must be unique")
        values = tuple(_find_collection(config, value) for value in collection_ids)
    else:
        values = tuple(value for value in config.collections if value.enabled)
    if not values:
        raise AnalysisError("catalogue selection contains no collections")
    return tuple(sorted(values, key=lambda value: value.id))


def _load_reader_input(
    config: Config,
    collection: CollectionConfig,
) -> _ReaderInput | str:
    try:
        current = current_reader_generation(config, collection.id)
    except AnalysisError as exc:
        message = str(exc)
        if (
            "no current derived reader" in message
            or "not catalogue-compatible" in message
            or "no canonical catalogue-compatible reader" in message
        ):
            return message
        raise
    access_path = current.generation_directory / "access.json"
    if not access_path.exists():
        return "current reader has no typed access graph"
    try:
        access = load_access_collection(current.generation_directory)
        validate_access_indexes(current.generation_directory)
    except ValueError as exc:
        raise AnalysisError(f"collection {collection.id} has an invalid access graph: {exc}") from exc
    if access.id != access_id(collection.id, "collection", ""):
        raise AnalysisError(f"collection {collection.id} access namespace does not match configuration")
    if access.status != current.metadata.get("status"):
        raise AnalysisError(f"collection {collection.id} reader and access statuses disagree")
    resolved_derived = config.project.derived_root.resolve()
    resolved_reader = current.generation_directory.resolve()
    if not resolved_reader.is_relative_to(resolved_derived):
        raise AnalysisError(f"collection {collection.id} reader escapes derived storage")
    return _ReaderInput(
        collection,
        current.generation_directory,
        resolved_reader.relative_to(resolved_derived).as_posix(),
        current.metadata,
        access,
        _sha256(access_path),
    )


def _unavailable_collection(collection: CollectionConfig, reason: str) -> dict[str, object]:
    return {
        "configured_id": collection.id,
        "label": collection.title,
        "enabled": collection.enabled,
        "description": collection.description,
        "rights": collection.rights_note,
        "access_state": "unavailable",
        "reason": reason,
    }


def _catalogue_projection(
    readers: Sequence[_ReaderInput],
    unavailable: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    collections: list[dict[str, object]] = []
    for value in readers:
        reader_prefix = f"../../{value.relative_root}"
        collections.append(
            {
                "configured_id": value.configured.id,
                "access_id": value.access.id,
                "label": value.access.label,
                "access_state": value.access.status,
                "rights": list(value.access.rights),
                "artifacts": [
                    {
                        "id": artifact.id,
                        "label": artifact.label,
                        "role": artifact.role,
                        "capture_path": artifact.capture_path,
                        "media_type": artifact.media_type,
                        **({"sha256": artifact.sha256} if artifact.sha256 else {}),
                        **({"container_id": artifact.container_id} if artifact.container_id else {}),
                        **({"member_path": artifact.member_path} if artifact.member_path else {}),
                    }
                    for artifact in sorted(value.access.artifacts, key=lambda item: item.id)
                ],
                "relations": [
                    {
                        "source_id": relation.source_id,
                        "relation": relation.relation,
                        "target_id": relation.target_id,
                    }
                    for relation in value.access.relations
                ],
                "reader": {
                    "capture_id": value.metadata["capture_id"],
                    "build_key": value.metadata["build_key"],
                    "route": f"{reader_prefix}/{value.access.route}",
                },
                "items": [
                    {
                        "id": item.id,
                        "label": item.label,
                        "type": item.item_type,
                        "route": f"{reader_prefix}/{item.route}",
                        "citation": item.citation,
                        "rights": list(item.rights),
                        "representations": [
                            {
                                "id": representation.id,
                                "label": representation.label,
                                "kind": representation.kind,
                                "language": representation.language,
                                "route": f"{reader_prefix}/{representation.route}",
                                "artifact_ids": list(representation.artifact_ids),
                            }
                            for representation in sorted(
                                item.representations, key=lambda item: item.id
                            )
                        ],
                    }
                    for item in sorted(value.access.items, key=lambda item: item.id)
                ],
            }
        )
    collections.extend(dict(value) for value in unavailable)
    collections.sort(key=lambda value: str(value["configured_id"]))
    return {"schema_version": CATALOGUE_SCHEMA_VERSION, "collections": collections}


def _extract_representation(root: Path, representation: AccessRepresentation) -> str:
    path_value, fragment = _split_route(representation.route)
    if fragment is None:
        raise AnalysisError(f"representation route has no fragment: {representation.id}")
    path = _safe_relative_file(root, path_value)
    if path.stat().st_size > MAX_HTML_BYTES:
        raise AnalysisError(f"representation HTML is unavailable: {representation.route}")
    parser = _RepresentationTextParser(fragment)
    try:
        with path.open("r", encoding="utf-8") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), ""):
                parser.feed(block)
        parser.close()
    except (OSError, UnicodeError) as exc:
        raise AnalysisError(f"cannot read representation HTML: {representation.route}") from exc
    if parser.matches != 1:
        raise AnalysisError(
            f"representation fragment occurs {parser.matches} times: {representation.route}"
        )
    return unicodedata.normalize("NFC", " ".join("".join(parser.text).split()))


class _RepresentationTextParser(HTMLParser):
    def __init__(self, target: str) -> None:
        super().__init__(convert_charrefs=True)
        self.target = target
        self.capture_tag: str | None = None
        self.capture_tag_depth = 0
        self.skip_tags: list[str] = []
        self.matches = 0
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id") == self.target:
            self.matches += 1
            if self.capture_tag is None:
                self.capture_tag = tag
                self.capture_tag_depth = 1
        elif self.capture_tag is not None and tag == self.capture_tag:
            self.capture_tag_depth += 1
        if self.capture_tag is not None:
            classes = frozenset((values.get("class") or "").split())
            if self.skip_tags:
                if tag not in VOID_TAGS:
                    self.skip_tags.append(tag)
            elif tag in EXCLUDED_TAGS or classes & EXCLUDED_CLASSES:
                if tag in BLOCK_TAGS:
                    self.text.append(" ")
                if tag not in VOID_TAGS:
                    self.skip_tags.append(tag)
            elif tag in BLOCK_TAGS:
                self.text.append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if self.capture_tag is not None:
            if self.skip_tags:
                if tag in self.skip_tags:
                    while self.skip_tags:
                        if self.skip_tags.pop() == tag:
                            break
            elif tag in BLOCK_TAGS:
                self.text.append(" ")
            if tag == self.capture_tag:
                self.capture_tag_depth -= 1
                if self.capture_tag_depth == 0:
                    self.capture_tag = None

    def handle_data(self, data: str) -> None:
        if self.capture_tag is not None and not self.skip_tags:
            self.text.append(data)


def _build_database(
    path: Path,
    readers: Sequence[_ReaderInput],
    build_key: str,
) -> tuple[str, int]:
    connection = sqlite3.connect(path)
    logical = hashlib.sha256()
    document_count = 0
    text_bytes = 0
    try:
        connection.executescript(
            f"""
PRAGMA application_id={CATALOGUE_APPLICATION_ID};
PRAGMA user_version={CATALOGUE_SCHEMA_VERSION};
PRAGMA encoding='UTF-8';
PRAGMA page_size=4096;
PRAGMA auto_vacuum=NONE;
PRAGMA foreign_keys=ON;
CREATE TABLE index_metadata(key TEXT PRIMARY KEY COLLATE BINARY,value_json TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE collections(id TEXT PRIMARY KEY COLLATE BINARY,access_id TEXT NOT NULL UNIQUE,label TEXT NOT NULL,status TEXT NOT NULL,reader_root TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE artifacts(id TEXT PRIMARY KEY COLLATE BINARY,collection_id TEXT NOT NULL REFERENCES collections(id),label TEXT NOT NULL,role TEXT NOT NULL,capture_path TEXT NOT NULL,media_type TEXT NOT NULL,sha256 TEXT,container_id TEXT,member_path TEXT) WITHOUT ROWID;
CREATE TABLE items(id TEXT PRIMARY KEY COLLATE BINARY,collection_id TEXT NOT NULL REFERENCES collections(id),label TEXT NOT NULL,item_type TEXT NOT NULL,route TEXT NOT NULL,citation TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE representations(id TEXT PRIMARY KEY COLLATE BINARY,item_id TEXT NOT NULL REFERENCES items(id),label TEXT NOT NULL,kind TEXT NOT NULL,language TEXT NOT NULL,route TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE representation_artifacts(representation_id TEXT NOT NULL REFERENCES representations(id),artifact_id TEXT NOT NULL REFERENCES artifacts(id),ordinal INTEGER NOT NULL,PRIMARY KEY(representation_id,artifact_id),UNIQUE(representation_id,ordinal)) WITHOUT ROWID;
CREATE TABLE rights(owner_id TEXT NOT NULL,ordinal INTEGER NOT NULL,text TEXT NOT NULL,PRIMARY KEY(owner_id,ordinal)) WITHOUT ROWID;
CREATE TABLE relations(collection_id TEXT NOT NULL REFERENCES collections(id),ordinal INTEGER NOT NULL,source_id TEXT NOT NULL,relation TEXT NOT NULL,target_id TEXT NOT NULL,PRIMARY KEY(collection_id,ordinal)) WITHOUT ROWID;
CREATE TABLE documents(docid INTEGER PRIMARY KEY,document_id TEXT NOT NULL UNIQUE,collection_id TEXT NOT NULL,item_id TEXT NOT NULL,representation_id TEXT NOT NULL UNIQUE,reader_root TEXT NOT NULL,route TEXT NOT NULL,title TEXT NOT NULL,representation_label TEXT NOT NULL,item_type TEXT NOT NULL,kind TEXT NOT NULL,language TEXT NOT NULL,citation TEXT NOT NULL,body TEXT NOT NULL,body_sha256 TEXT NOT NULL);
CREATE INDEX documents_collection ON documents(collection_id,docid);
CREATE INDEX documents_language ON documents(language,docid);
CREATE INDEX documents_kind ON documents(kind,docid);
CREATE VIRTUAL TABLE document_fts USING fts5(title,representation_label,body,content='documents',content_rowid='docid',tokenize='unicode61 remove_diacritics 0',detail=full);
"""
        )
        connection.execute(
            "INSERT INTO index_metadata VALUES (?,?)",
            ("build_key", json.dumps(build_key)),
        )
        for value in readers:
            connection.execute(
                "INSERT INTO collections VALUES (?,?,?,?,?)",
                (
                    value.configured.id,
                    value.access.id,
                    value.access.label,
                    value.access.status,
                    value.relative_root,
                ),
            )
            for ordinal, right in enumerate(value.access.rights):
                connection.execute(
                    "INSERT INTO rights VALUES (?,?,?)",
                    (value.access.id, ordinal, right),
                )
            for artifact in sorted(value.access.artifacts, key=lambda item: item.id):
                connection.execute(
                    "INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        artifact.id,
                        value.configured.id,
                        artifact.label,
                        artifact.role,
                        artifact.capture_path,
                        artifact.media_type,
                        artifact.sha256,
                        artifact.container_id,
                        artifact.member_path,
                    ),
                )
            for ordinal, relation in enumerate(value.access.relations):
                connection.execute(
                    "INSERT INTO relations VALUES (?,?,?,?,?)",
                    (
                        value.configured.id,
                        ordinal,
                        relation.source_id,
                        relation.relation,
                        relation.target_id,
                    ),
                )
            for item in sorted(value.access.items, key=lambda item: item.id):
                connection.execute(
                    "INSERT INTO items VALUES (?,?,?,?,?,?)",
                    (
                        item.id,
                        value.configured.id,
                        item.label,
                        item.item_type,
                        item.route,
                        item.citation,
                    ),
                )
                for ordinal, right in enumerate(item.rights):
                    connection.execute(
                        "INSERT INTO rights VALUES (?,?,?)",
                        (item.id, ordinal, right),
                    )
                for representation in sorted(item.representations, key=lambda item: item.id):
                    connection.execute(
                        "INSERT INTO representations VALUES (?,?,?,?,?,?)",
                        (
                            representation.id,
                            item.id,
                            representation.label,
                            representation.kind,
                            representation.language,
                            representation.route,
                        ),
                    )
                    for ordinal, artifact_id_value in enumerate(representation.artifact_ids):
                        connection.execute(
                            "INSERT INTO representation_artifacts VALUES (?,?,?)",
                            (representation.id, artifact_id_value, ordinal),
                        )
                    body = _extract_representation(value.root, representation)
                    text_bytes += len(body.encode("utf-8"))
                    if text_bytes > MAX_SEARCH_TEXT_BYTES:
                        raise ValueError(
                            f"catalogue searchable text exceeds {MAX_SEARCH_TEXT_BYTES} bytes"
                        )
                    document_count += 1
                    document = {
                        "document_id": representation.id,
                        "collection_id": value.configured.id,
                        "item_id": item.id,
                        "representation_id": representation.id,
                        "reader_root": value.relative_root,
                        "route": representation.route,
                        "title": unicodedata.normalize("NFC", item.label),
                        "representation_label": unicodedata.normalize(
                            "NFC", representation.label
                        ),
                        "item_type": item.item_type,
                        "kind": representation.kind,
                        "language": representation.language,
                        "citation": unicodedata.normalize("NFC", item.citation),
                        "body": body,
                        "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                    }
                    logical.update(_canonical_json(document))
                    logical.update(b"\n")
                    connection.execute(
                        "INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            document_count,
                            *(document[key] for key in (
                                "document_id", "collection_id", "item_id",
                                "representation_id", "reader_root", "route", "title",
                                "representation_label", "item_type", "kind", "language",
                                "citation", "body", "body_sha256",
                            )),
                        ),
                    )
        connection.execute(
            "INSERT INTO document_fts(rowid,title,representation_label,body) "
            "SELECT docid,title,representation_label,body FROM documents ORDER BY docid"
        )
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()
    return logical.hexdigest(), document_count


def _validate_database(path: Path, *, verify_external_content: bool = False) -> None:
    connection = (
        sqlite3.connect(path)
        if verify_external_content
        else sqlite3.connect(_sqlite_readonly_uri(path), uri=True)
    )
    try:
        disable_extensions = getattr(connection, "enable_load_extension", None)
        if disable_extensions is not None:
            disable_extensions(False)
        if connection.execute("PRAGMA application_id").fetchone()[0] != CATALOGUE_APPLICATION_ID:
            raise ValueError("catalogue database application ID is invalid")
        if connection.execute("PRAGMA user_version").fetchone()[0] != CATALOGUE_SCHEMA_VERSION:
            raise ValueError("catalogue database schema version is invalid")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("catalogue database quick check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("catalogue database foreign key check failed")
        document_count = connection.execute("SELECT count(*) FROM documents").fetchone()[0]
        fts_count = connection.execute("SELECT count(*) FROM document_fts").fetchone()[0]
        if document_count != fts_count:
            raise ValueError("catalogue database document counts do not match")
        if verify_external_content:
            connection.execute(
                "INSERT INTO document_fts(document_fts, rank) "
                "VALUES ('integrity-check', 1)"
            )
    finally:
        connection.close()


def _sqlite_identity() -> dict[str, object]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE VIRTUAL TABLE fts_probe USING fts5(value)")
        source_id = connection.execute("SELECT sqlite_source_id()").fetchone()[0]
        options = sorted(row[0] for row in connection.execute("PRAGMA compile_options"))
    except sqlite3.Error as exc:
        raise AnalysisError(f"SQLite FTS5 is unavailable: {exc}") from exc
    finally:
        connection.close()
    return {
        "version": sqlite3.sqlite_version,
        "source_id": source_id,
        "compile_options": options,
    }


def _render_catalogue(
    projection: Mapping[str, object],
    status: str,
    warnings: Sequence[str],
) -> str:
    sections: list[str] = []
    collections = projection.get("collections", [])
    for value in collections if isinstance(collections, list) else []:
        if not isinstance(value, dict):
            continue
        label = escape_html(str(value.get("label", value.get("configured_id", "Collection"))))
        state = escape_html(str(value.get("access_state", "unavailable")))
        if "reader" not in value:
            sections.append(
                f'<section class="catalogue-card unavailable"><p class="reader-eyebrow">{state}</p>'
                f"<h2>{label}</h2><p>{escape_html(str(value.get('description', '')))}</p>"
                f"<p>{escape_html(str(value.get('reason', 'No compatible reader is current.')))}</p></section>"
            )
            continue
        items = value.get("items", [])
        entries: list[str] = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            representations = item.get("representations", [])
            badges = " ".join(
                f'<span>{escape_html(str(rep.get("kind", "")))}/{escape_html(str(rep.get("language", "")))}</span>'
                for rep in representations
                if isinstance(rep, dict)
            ) if isinstance(representations, list) else ""
            entries.append(
                f'<li><a href="{escape_html(str(item.get("route", "")), quote=True)}">'
                f'{escape_html(str(item.get("label", "")))}</a><span class="representation-badges">'
                f"{badges}</span></li>"
            )
        reader = value["reader"]
        assert isinstance(reader, dict)
        sections.append(
            f'<section class="catalogue-card"><p class="reader-eyebrow">{state}</p><h2>{label}</h2>'
            f'<p><a href="{escape_html(str(reader["route"]), quote=True)}">Open collection reader</a></p>'
            f'<ol class="catalogue-items">{"".join(entries)}</ol></section>'
        )
    return render_document(
        "Text Preserver collection catalogue",
        f"""
{render_navigation((ReaderLink("Catalogue", "index.html"),))}
<header class="reader-header"><p class="reader-eyebrow">Derived access snapshot</p>
<h1>Collection catalogue</h1><p class="reader-lede">Browse stable collection and item links across compatible current readers.</p></header>
<main class="reader-main">
  {render_status(status, warnings)}
  {render_notice("Full-text search is available from the local command line with text-preserver search. The SQLite index is a rebuildable derivative; captures and reader generations remain authoritative.")}
  <div class="catalogue-grid">{"".join(sections)}</div>
</main>
""",
        collection_stylesheet="catalogue.css",
    )


def _catalogue_stylesheet() -> str:
    return """.catalogue-grid{display:grid;gap:1.5rem}.catalogue-card{padding:1.25rem;border:1px solid
var(--reader-rule);background:var(--reader-sheet)}.catalogue-card.unavailable{border-style:dashed}
.catalogue-items{list-style:none;padding:0}.catalogue-items li{display:grid;grid-template-columns:1fr auto;
gap:1rem;padding:.55rem 0;border-top:1px solid var(--reader-rule)}.representation-badges{display:flex;
gap:.35rem;flex-wrap:wrap;color:var(--reader-muted);font-size:.75rem}.representation-badges span{
border:1px solid var(--reader-rule);padding:.1rem .35rem}@media(max-width:700px){.catalogue-items li{
grid-template-columns:1fr}}"""


def _catalogue_identity() -> dict[str, object]:
    return {
        "schema_version": CATALOGUE_SCHEMA_VERSION,
        "sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


def _validate_catalogue_tree(root: Path, *, allow_metadata: bool = False) -> dict[str, Any]:
    if root.is_symlink() or not root.is_dir():
        raise AnalysisError("catalogue output is not a regular directory")
    entries: list[dict[str, Any]] = []
    total = 0
    files = 0
    directories = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        info = path.lstat()
        relative = path.relative_to(root).as_posix()
        if stat.S_ISLNK(info.st_mode):
            raise AnalysisError(f"catalogue output contains a symlink: {relative}")
        if stat.S_ISDIR(info.st_mode):
            directories += 1
            entries.append({"path": relative, "type": "directory"})
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise AnalysisError(f"catalogue output contains an unsafe file: {relative}")
        if allow_metadata and relative == "metadata.json":
            continue
        files += 1
        total += info.st_size
        if files > MAX_CATALOGUE_FILES or total > MAX_CATALOGUE_BYTES:
            raise AnalysisError("catalogue output exceeds its file or byte limit")
        if path.suffix != ".sqlite":
            try:
                path.read_text(encoding="utf-8")
            except UnicodeError as exc:
                raise AnalysisError(f"catalogue text output is not UTF-8: {relative}") from exc
        entries.append(
            {"path": relative, "type": "file", "size": info.st_size, "sha256": _sha256(path)}
        )
    if not (root / "index.html").is_file() or not (root / "catalogue.sqlite").is_file():
        raise AnalysisError("catalogue output is missing its index or database")
    return {
        "sha256": hashlib.sha256(_canonical_json(entries)).hexdigest(),
        "file_count": files,
        "directory_count": directories,
        "total_bytes": total,
    }


@contextmanager
def _catalogue_lock(derived_root: Path) -> Iterator[None]:
    path = derived_root / ".catalogue.lock"
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise AnalysisError(f"catalogue lock is unsafe: {path}")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _publish_catalogue(
    derived_root: Path,
    staging: Path,
    metadata: Mapping[str, Any],
) -> tuple[Path, dict[str, Any]]:
    generations = derived_root / "catalogue-generations"
    if staging.is_symlink() or not staging.resolve().is_relative_to(generations.resolve()):
        raise AnalysisError("catalogue staging path escapes its generation root")
    candidate = dict(metadata)
    (staging / "metadata.json").write_text(
        json.dumps(candidate, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    generation = generations / str(candidate["build_key"])
    if generation.is_symlink():
        _quarantine_catalogue(derived_root, staging, candidate, "generation path is a symlink")
    if generation.exists():
        try:
            existing = _read_json(generation / "metadata.json", "catalogue metadata")
            tree = _validate_catalogue_tree(generation, allow_metadata=True)
            for key in (
                "schema_version", "status", "build_key", "build_inputs", "output_tree",
                "database", "summary", "warnings",
            ):
                expected = tree if key == "output_tree" else candidate.get(key)
                if existing.get(key) != expected:
                    raise AnalysisError(f"catalogue reproducibility mismatch for {key}")
        except AnalysisError as exc:
            _quarantine_catalogue(derived_root, staging, candidate, str(exc))
        shutil.rmtree(staging)
        _make_tree_read_only(generation)
        _fsync_tree(generation)
        return generation, existing
    try:
        _fsync_tree(staging)
        os.rename(staging, generation)
        _make_tree_read_only(generation)
        _fsync_tree(generation)
        _fsync_directory(generations)
    except OSError as exc:
        if generation.exists() and not generation.is_symlink():
            _make_tree_writable(generation)
            shutil.rmtree(generation)
        raise AnalysisError(f"cannot publish catalogue generation: {exc}") from exc
    return generation, candidate


def _quarantine_catalogue(
    derived_root: Path,
    staging: Path,
    metadata: Mapping[str, Any],
    reason: str,
) -> None:
    (staging / "reproducibility.json").write_text(
        json.dumps({"build_key": metadata["build_key"], "reason": reason}, indent=2) + "\n",
        encoding="utf-8",
    )
    root = _ensure_derived_directory(derived_root, Path("catalogue-quarantine"))
    target = root / f"{metadata['build_key']}-{secrets.token_hex(6)}"
    os.rename(staging, target)
    _make_tree_read_only(target)
    raise AnalysisError(
        f"catalogue reproducibility failure: key={metadata['build_key']}; reason={reason}"
    )


def _existing_catalogue_index(derived_root: Path) -> Path | None:
    if derived_root.is_symlink() or (derived_root.exists() and not derived_root.is_dir()):
        raise AnalysisError(f"derived root is unsafe: {derived_root}")
    pointer = derived_root / "LATEST-CATALOGUE"
    if not (pointer.exists() or pointer.is_symlink()):
        return None
    if pointer.is_symlink() or not pointer.is_file():
        raise AnalysisError(f"catalogue pointer is not a regular file: {pointer}")
    try:
        relative = Path(pointer.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeError) as exc:
        raise AnalysisError(f"cannot read catalogue pointer: {exc}") from exc
    if (
        relative.is_absolute()
        or len(relative.parts) != 2
        or relative.parts[0] != "catalogue-generations"
        or not _is_sha256(relative.parts[1])
    ):
        raise AnalysisError(f"unsafe catalogue pointer content: {pointer}")
    generations = derived_root / "catalogue-generations"
    if generations.is_symlink() or not generations.is_dir():
        raise AnalysisError(f"catalogue generations directory is unsafe: {generations}")
    generation = derived_root / relative
    if (
        generation.is_symlink()
        or not generation.is_dir()
        or not generation.resolve().is_relative_to(derived_root.resolve())
    ):
        raise AnalysisError(f"catalogue pointer target is unavailable: {pointer}")
    metadata = _read_json(generation / "metadata.json", "catalogue metadata")
    if (
        metadata.get("schema_version") != CATALOGUE_SCHEMA_VERSION
        or metadata.get("build_key") != relative.parts[1]
        or metadata.get("status") not in SUCCESS_STATUSES
        or not isinstance(metadata.get("build_inputs"), dict)
        or hashlib.sha256(_canonical_json(metadata["build_inputs"])).hexdigest()
        != relative.parts[1]
        or metadata.get("output_tree")
        != _validate_catalogue_tree(generation, allow_metadata=True)
    ):
        raise AnalysisError(f"catalogue metadata does not match pointer: {pointer}")
    _validate_database(generation / "catalogue.sqlite")
    return generation / "index.html"


def _split_route(value: str) -> tuple[str, str | None]:
    path, marker, fragment = value.partition("#")
    return path, fragment if marker else None


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _safe_relative_directory(root: Path, relative: str) -> Path:
    if not isinstance(relative, str):
        raise AnalysisError("catalogue reader root is invalid")
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != relative:
        raise AnalysisError(f"catalogue reader root is unsafe: {relative}")
    current = root
    if current.is_symlink() or not current.is_dir():
        raise AnalysisError(f"catalogue storage root is unsafe: {root}")
    for part in path.parts:
        current = current / part
        try:
            info = current.lstat()
        except OSError as exc:
            raise AnalysisError(f"catalogue reader root is unavailable: {relative}") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise AnalysisError(f"catalogue reader root is unsafe: {relative}")
    if not current.resolve().is_relative_to(root.resolve()):
        raise AnalysisError(f"catalogue reader root escapes storage: {relative}")
    return current


def _safe_relative_file(root: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    if (
        not relative
        or path.is_absolute()
        or ".." in path.parts
        or path.as_posix() != relative
    ):
        raise AnalysisError(f"catalogue file route is unsafe: {relative}")
    current = root
    if current.is_symlink() or not current.is_dir():
        raise AnalysisError(f"catalogue file root is unsafe: {root}")
    for index, part in enumerate(path.parts):
        current = current / part
        try:
            info = current.lstat()
        except OSError as exc:
            raise AnalysisError(f"catalogue file is unavailable: {relative}") from exc
        final = index == len(path.parts) - 1
        if stat.S_ISLNK(info.st_mode):
            raise AnalysisError(f"catalogue file route contains a symlink: {relative}")
        if final:
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise AnalysisError(f"catalogue file is unsafe: {relative}")
        elif not stat.S_ISDIR(info.st_mode):
            raise AnalysisError(f"catalogue file route is unavailable: {relative}")
    if not current.resolve().is_relative_to(root.resolve()):
        raise AnalysisError(f"catalogue file route escapes its reader: {relative}")
    return current


def _sqlite_readonly_uri(path: Path) -> str:
    return path.resolve().as_uri() + "?mode=ro&immutable=1"
