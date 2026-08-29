"""Create and independently validate offline WACZ 1.1.1 packages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
from typing import Any, Iterator, Mapping, Sequence
from urllib.parse import urlsplit
import zipfile
import zlib

from text_preserver import __version__
from text_preserver.preservation.payload_roles import (
    ExportPolicy,
    ExportProfile,
    PayloadRole,
    PayloadRoleError,
    VerifiedCapture,
    assert_capture_unchanged,
)


WACZ_VERSION = "1.1.1"
_JSON_LIMIT = 16 * 1024 * 1024
_CDX_LIMIT = 256 * 1024 * 1024
_WARC_RECORD_LIMIT = 512 * 1024 * 1024
_WARC_RECORD_COUNT_LIMIT = 1_000_000
_WARC_TYPES = frozenset({"response", "resource", "revisit"})


class WaczError(RuntimeError):
    """Raised when a WACZ cannot be created safely."""


@dataclass(frozen=True)
class WaczMetadata:
    title: str
    description: str
    created: str
    main_page_url: str | None = None
    main_page_date: str | None = None


@dataclass(frozen=True)
class WaczCreationResult:
    path: Path
    profile: ExportProfile
    policy_identifier: str
    warc_files: int
    indexed_records: int
    pages: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "profile": self.profile.value,
            "policy_identifier": self.policy_identifier,
            "warc_files": self.warc_files,
            "indexed_records": self.indexed_records,
            "pages": self.pages,
        }


@dataclass(frozen=True)
class WaczValidationResult:
    path: Path
    checked_resources: int
    indexed_records: int
    pages: int
    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "ok": self.ok,
            "checked_resources": self.checked_resources,
            "indexed_records": self.indexed_records,
            "pages": self.pages,
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class _WarcRecord:
    url: str
    timestamp: str
    mime: str
    status: str
    digest: str
    offset: int
    length: int
    filename: str


def create_wacz(
    capture: VerifiedCapture,
    destination: str | Path,
    *,
    warc_paths: Sequence[str],
    profile: ExportProfile | str,
    policy: ExportPolicy,
    metadata: WaczMetadata,
) -> WaczCreationResult:
    """Atomically derive a WACZ from explicitly selected capture WARC files."""
    resolved_profile = _profile(profile)
    _validate_metadata(metadata)
    try:
        assert_capture_unchanged(capture)
        policy_paths = policy.selected_paths(resolved_profile, capture.files)
    except PayloadRoleError as exc:
        raise WaczError(str(exc)) from exc
    normalized_paths = tuple(_safe_capture_path(path) for path in warc_paths)
    if not normalized_paths or len(normalized_paths) != len(set(normalized_paths)):
        raise WaczError("WACZ requires one or more unique WARC paths")
    for relative in normalized_paths:
        if relative not in policy_paths:
            raise WaczError(f"WARC path is not permitted by export policy: {relative}")
        try:
            role = capture.role_for(relative)
        except KeyError as exc:
            raise WaczError(f"unknown capture WARC path: {relative}") from exc
        if role is not PayloadRole.PRESERVATION_ORIGINAL or not _is_warc_name(relative):
            raise WaczError(f"path is not a preservation-original WARC: {relative}")

    requested_target = Path(destination).expanduser()
    if requested_target.exists() or requested_target.is_symlink():
        raise WaczError(f"WACZ destination already exists: {requested_target}")
    target = requested_target.parent.resolve() / requested_target.name
    if target.suffix.lower() != ".wacz":
        raise WaczError("WACZ destination must use the .wacz extension")
    if not target.parent.is_dir():
        raise WaczError(f"WACZ destination parent does not exist: {target.parent}")

    archive_names = _archive_names(normalized_paths)
    source_snapshots: dict[str, tuple[int, int, str]] = {}
    staged_sources: dict[str, Path] = {}
    records: list[_WarcRecord] = []
    staging_directory = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.warcs.", dir=target.parent)
    )
    try:
        for index, (relative, archive_name) in enumerate(
            zip(normalized_paths, archive_names, strict=True)
        ):
            source = capture.directory / PurePosixPath(relative)
            source_snapshots[relative] = _snapshot(source)
            staged = staging_directory / f"{index:04d}-{Path(archive_name).name}"
            _copy_verified_warc(source, staged, capture.file_sha256[relative])
            staged_sources[relative] = staged
            records.extend(
                _warc_records(
                    staged,
                    archive_name,
                    max_records=_WARC_RECORD_COUNT_LIMIT - len(records),
                )
            )
    except Exception:
        shutil.rmtree(staging_directory, ignore_errors=True)
        raise
    if not records:
        shutil.rmtree(staging_directory, ignore_errors=True)
        raise WaczError("selected WARCs contain no replayable response, resource, or revisit records")
    try:
        records.sort(
            key=lambda item: (
                _surt(item.url),
                _cdx_timestamp(item.timestamp),
                item.filename,
                item.offset,
            )
        )
        cdx = _cdx_bytes(records)
        compressed_cdx = gzip.compress(cdx, compresslevel=9, mtime=0)
        page_values = _pages(records)
        pages = _pages_bytes(page_values)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", dir=target.parent
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
    except Exception:
        shutil.rmtree(staging_directory, ignore_errors=True)
        raise
    try:
        resources: list[dict[str, Any]] = []
        with zipfile.ZipFile(temporary, "w", allowZip64=True) as package:
            for relative, archive_name in zip(normalized_paths, archive_names, strict=True):
                source = staged_sources[relative]
                package.write(source, f"archive/{archive_name}", compress_type=zipfile.ZIP_STORED)
                resources.append(_resource(source, f"archive/{archive_name}"))
            _write_zip_bytes(package, "indexes/index.cdx.gz", compressed_cdx, zipfile.ZIP_STORED)
            resources.append(_bytes_resource(compressed_cdx, "indexes/index.cdx.gz"))
            _write_zip_bytes(package, "pages/pages.jsonl", pages, zipfile.ZIP_DEFLATED)
            resources.append(_bytes_resource(pages, "pages/pages.jsonl"))

            datapackage: dict[str, Any] = {
                "profile": "data-package",
                "wacz_version": WACZ_VERSION,
                "title": metadata.title,
                "description": metadata.description,
                "created": _rfc3339(metadata.created),
                "software": f"text-preserver {__version__}",
                "resources": resources,
                "text_preserver": {
                    "capture_manifest_sha256": capture.manifest_sha256,
                    "export_profile": resolved_profile.value,
                    "export_policy": policy.identifier,
                    "source_paths": list(normalized_paths),
                },
            }
            if metadata.main_page_url is not None:
                datapackage["mainPageUrl"] = _url(metadata.main_page_url)
            if metadata.main_page_date is not None:
                datapackage["mainPageDate"] = _rfc3339(metadata.main_page_date)
            datapackage_bytes = _json_bytes(datapackage)
            _write_zip_bytes(package, "datapackage.json", datapackage_bytes, zipfile.ZIP_DEFLATED)
            digest = {
                "path": "datapackage.json",
                "hash": f"sha256:{hashlib.sha256(datapackage_bytes).hexdigest()}",
            }
            _write_zip_bytes(
                package,
                "datapackage-digest.json",
                _json_bytes(digest),
                zipfile.ZIP_DEFLATED,
            )
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())

        validation = validate_wacz(temporary)
        if not validation.ok:
            raise WaczError("created WACZ failed validation: " + "; ".join(validation.errors))
        try:
            assert_capture_unchanged(capture)
        except PayloadRoleError as exc:
            raise WaczError(str(exc)) from exc
        for relative, snapshot in source_snapshots.items():
            if _snapshot(capture.directory / PurePosixPath(relative)) != snapshot:
                raise WaczError(f"source WARC changed during export: {relative}")
        os.link(temporary, target)
        temporary.unlink()
        _fsync_directory(target.parent)
    finally:
        if temporary.exists():
            temporary.unlink()
        shutil.rmtree(staging_directory, ignore_errors=True)
    return WaczCreationResult(
        path=target,
        profile=resolved_profile,
        policy_identifier=policy.identifier,
        warc_files=len(normalized_paths),
        indexed_records=len(records),
        pages=len(page_values),
    )


def validate_wacz(path: str | Path) -> WaczValidationResult:
    """Validate WACZ structure, resources, indexes, pages, and package digest."""
    requested_target = Path(path).expanduser()
    if requested_target.is_symlink():
        return WaczValidationResult(requested_target, 0, 0, 0, ("WACZ must be a regular file",))
    target = requested_target.resolve()
    errors: list[str] = []
    checked = 0
    index_count = 0
    page_count = 0
    if not target.is_file() or target.is_symlink():
        return WaczValidationResult(target, 0, 0, 0, ("WACZ must be a regular file",))
    try:
        with zipfile.ZipFile(target) as package:
            infos = package.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                errors.append("ZIP contains duplicate member names")
            for info in infos:
                try:
                    _safe_zip_path(info.filename)
                except WaczError as exc:
                    errors.append(str(exc))
                if info.flag_bits & 0x1:
                    errors.append(f"encrypted ZIP member is not supported: {info.filename}")
                if info.is_dir():
                    errors.append(f"WACZ contains an explicit directory member: {info.filename}")
            by_name = {info.filename: info for info in infos if not info.is_dir()}
            required = {
                "datapackage.json",
                "datapackage-digest.json",
                "indexes/index.cdx.gz",
                "pages/pages.jsonl",
            }
            for missing in sorted(required - by_name.keys()):
                errors.append(f"missing required WACZ member: {missing}")
            archive_names = sorted(
                name for name in by_name if name.startswith("archive/") and _is_warc_name(name)
            )
            if not archive_names:
                errors.append("WACZ archive/ contains no WARC files")
            for name in by_name:
                if name.startswith("archive/") and name not in archive_names:
                    errors.append(f"unsupported member in archive/: {name}")
                if name.startswith("indexes/") and name != "indexes/index.cdx.gz":
                    errors.append(f"unsupported member in indexes/: {name}")
                if name.startswith("pages/") and name != "pages/pages.jsonl":
                    errors.append(f"unsupported member in pages/: {name}")
            unsafe_hash_members: set[str] = set()
            for name in archive_names + ["indexes/index.cdx.gz"]:
                stored_info = by_name.get(name)
                if stored_info is not None and stored_info.compress_type != zipfile.ZIP_STORED:
                    errors.append(f"already-compressed/random-access member is not stored: {name}")
                    unsafe_hash_members.add(name)
            for name, limit in (
                ("indexes/index.cdx.gz", _CDX_LIMIT),
                ("pages/pages.jsonl", _JSON_LIMIT),
            ):
                bounded_info = by_name.get(name)
                if bounded_info is not None and bounded_info.file_size > limit:
                    unsafe_hash_members.add(name)

            datapackage = _zip_json(package, by_name.get("datapackage.json"), errors)
            digest_value = _zip_json(package, by_name.get("datapackage-digest.json"), errors)
            if (
                isinstance(digest_value, dict)
                and "datapackage.json" in by_name
                and by_name["datapackage.json"].file_size <= _JSON_LIMIT
            ):
                wanted = digest_value.get("hash")
                actual = "sha256:" + _zip_hash(package, by_name["datapackage.json"])
                if digest_value.get("path") != "datapackage.json" or wanted != actual:
                    errors.append("datapackage-digest.json does not verify datapackage.json")
            if isinstance(datapackage, dict):
                if datapackage.get("profile") != "data-package":
                    errors.append("datapackage profile must be data-package")
                if datapackage.get("wacz_version") != WACZ_VERSION:
                    errors.append(f"datapackage wacz_version must be {WACZ_VERSION}")
                resources = datapackage.get("resources")
                if not isinstance(resources, list):
                    errors.append("datapackage resources must be an array")
                else:
                    listed: set[str] = set()
                    for index, resource in enumerate(resources):
                        if not isinstance(resource, dict):
                            errors.append(f"resources[{index}] must be an object")
                            continue
                        resource_path = resource.get("path")
                        if not isinstance(resource_path, str):
                            errors.append(f"resources[{index}].path must be a string")
                            continue
                        try:
                            _safe_zip_path(resource_path)
                        except WaczError as exc:
                            errors.append(str(exc))
                            continue
                        if resource_path in listed:
                            errors.append(f"duplicate datapackage resource: {resource_path}")
                            continue
                        listed.add(resource_path)
                        resource_info = by_name.get(resource_path)
                        if resource_info is None:
                            errors.append(f"datapackage resource is missing: {resource_path}")
                            continue
                        if resource.get("bytes") != resource_info.file_size:
                            errors.append(f"resource byte count mismatch: {resource_path}")
                        if resource_path in unsafe_hash_members:
                            continue
                        wanted = resource.get("hash")
                        actual = "sha256:" + _zip_hash(package, resource_info)
                        checked += 1
                        if wanted != actual:
                            errors.append(f"resource hash mismatch: {resource_path}")
                    actual_resources = set(by_name) - {
                        "datapackage.json",
                        "datapackage-digest.json",
                    }
                    for missing in sorted(actual_resources - listed):
                        errors.append(f"WACZ member is not listed as a resource: {missing}")
                    for unexpected in sorted(listed - actual_resources):
                        errors.append(f"datapackage lists a non-resource member: {unexpected}")

            if "pages/pages.jsonl" in by_name:
                page_count = _validate_pages(
                    _zip_read(package, by_name["pages/pages.jsonl"], _JSON_LIMIT), errors
                )
            if "indexes/index.cdx.gz" in by_name:
                raw_index = _zip_read(package, by_name["indexes/index.cdx.gz"], _CDX_LIMIT)
                try:
                    cdx = _gzip_decompress_bounded(raw_index, _CDX_LIMIT, "CDXJ")
                    index_count = _validate_cdx(
                        package,
                        cdx,
                        by_name,
                        unsafe_hash_members,
                        errors,
                    )
                except (OSError, EOFError, zlib.error, WaczError) as exc:
                    errors.append(f"invalid CDXJ index: {exc}")
    except (OSError, zipfile.BadZipFile, RuntimeError, WaczError) as exc:
        errors.append(f"invalid WACZ ZIP: {exc}")
    return WaczValidationResult(target, checked, index_count, page_count, tuple(errors))


def _warc_records(
    path: Path,
    archive_name: str,
    *,
    max_records: int = _WARC_RECORD_COUNT_LIMIT,
) -> list[_WarcRecord]:
    records: list[_WarcRecord] = []
    if path.name.lower().endswith(".gz"):
        for offset, length, data in _gzip_members(path):
            records.extend(
                _parse_warc_data(
                    data,
                    archive_name,
                    offset,
                    length,
                    max_records=max_records - len(records),
                )
            )
    else:
        if path.stat().st_size > _WARC_RECORD_LIMIT:
            raise WaczError(
                f"uncompressed WARC exceeds {_WARC_RECORD_LIMIT} byte parsing limit: {path}"
            )
        data = _read_regular(path)
        records.extend(
            _parse_warc_data(
                data,
                archive_name,
                None,
                None,
                max_records=max_records,
            )
        )
    return records


def _gzip_members(path: Path) -> Iterator[tuple[int, int, bytes]]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise WaczError(f"expected a regular WARC file: {path}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        pending = b""
        position = 0
        while position < info.st_size:
            decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
            content = bytearray()
            consumed = 0
            while not decompressor.eof:
                if not pending:
                    pending = stream.read(1024 * 1024)
                    if not pending:
                        raise WaczError(f"truncated gzip WARC: {path}")
                try:
                    produced = decompressor.decompress(
                        pending,
                        _WARC_RECORD_LIMIT + 1 - len(content),
                    )
                except zlib.error as exc:
                    raise WaczError(f"invalid gzip WARC {path}: {exc}") from exc
                content.extend(produced)
                if len(content) > _WARC_RECORD_LIMIT:
                    raise WaczError(
                        f"decompressed WARC record exceeds {_WARC_RECORD_LIMIT} bytes: {path}"
                    )
                if decompressor.eof:
                    leftover = decompressor.unused_data
                else:
                    leftover = decompressor.unconsumed_tail
                used = len(pending) - len(leftover)
                if used <= 0 and not decompressor.eof:
                    raise WaczError(
                        f"decompressed WARC record exceeds {_WARC_RECORD_LIMIT} bytes: {path}"
                    )
                consumed += used
                pending = leftover
            if consumed <= 0:
                raise WaczError(f"invalid gzip member in WARC: {path}")
            yield position, consumed, bytes(content)
            position += consumed


def _parse_warc_data(
    data: bytes,
    filename: str,
    member_offset: int | None,
    member_length: int | None,
    *,
    max_records: int = _WARC_RECORD_COUNT_LIMIT,
) -> list[_WarcRecord]:
    records: list[_WarcRecord] = []
    position = 0
    while position < len(data):
        while data[position : position + 2] == b"\r\n":
            position += 2
        while data[position : position + 1] == b"\n":
            position += 1
        if position >= len(data):
            break
        start = position
        header_end, separator = _header_end(data, position)
        header_lines = data[position:header_end].splitlines()
        if not header_lines or header_lines[0] not in {b"WARC/1.0", b"WARC/1.1"}:
            raise WaczError(f"invalid WARC record header in {filename} at offset {start}")
        headers = _headers(header_lines[1:], filename)
        try:
            content_length = int(headers[b"content-length"])
        except (KeyError, ValueError) as exc:
            raise WaczError(f"invalid WARC Content-Length in {filename} at offset {start}") from exc
        if content_length < 0:
            raise WaczError(f"negative WARC Content-Length in {filename}")
        payload_start = header_end + separator
        payload_end = payload_start + content_length
        if payload_end > len(data):
            raise WaczError(f"truncated WARC record in {filename} at offset {start}")
        position = payload_end
        while data[position : position + 2] == b"\r\n":
            position += 2
        while data[position : position + 1] == b"\n":
            position += 1
        record_type = headers.get(b"warc-type", b"").decode("ascii", "replace").lower()
        target = headers.get(b"warc-target-uri")
        warc_date = headers.get(b"warc-date")
        if record_type not in _WARC_TYPES or target is None or warc_date is None:
            continue
        target_value = target.decode("utf-8")
        if target_value.startswith("<") and target_value.endswith(">"):
            target_value = target_value[1:-1]
        if urlsplit(target_value).scheme.lower() not in {"http", "https"}:
            continue
        if len(records) >= max_records:
            raise WaczError(f"WACZ source exceeds {_WARC_RECORD_COUNT_LIMIT} replayable records")
        url = _url(target_value)
        timestamp = _rfc3339(warc_date.decode("ascii"))
        block = data[payload_start:payload_end]
        mime, status, entity = _record_http_metadata(record_type, headers, block)
        digest = headers.get(b"warc-payload-digest")
        digest_value = (
            digest.decode("ascii").lower()
            if digest is not None
            else "sha256:" + hashlib.sha256(entity).hexdigest()
        )
        records.append(
            _WarcRecord(
                url=url,
                timestamp=timestamp,
                mime=mime,
                status=status,
                digest=digest_value,
                offset=member_offset if member_offset is not None else start,
                length=member_length if member_length is not None else position - start,
                filename=filename,
            )
        )
    return records


def _record_http_metadata(
    record_type: str,
    headers: Mapping[bytes, bytes],
    block: bytes,
) -> tuple[str, str, bytes]:
    mime = headers.get(b"content-type", b"application/octet-stream").decode(
        "latin-1", "replace"
    ).split(";", 1)[0]
    status = "-"
    entity = block
    if record_type in {"response", "revisit"} and block.startswith(b"HTTP/"):
        end, separator = _header_end(block, 0)
        lines = block[:end].splitlines()
        parts = lines[0].split(None, 2)
        if len(parts) >= 2:
            status = parts[1].decode("ascii", "replace")
        http_headers = _headers(lines[1:], "HTTP response")
        mime = http_headers.get(b"content-type", mime.encode("latin-1")).decode(
            "latin-1", "replace"
        ).split(";", 1)[0]
        entity = block[end + separator :]
    return mime or "application/octet-stream", status, entity


def _headers(lines: Sequence[bytes], context: str) -> dict[bytes, bytes]:
    headers: dict[bytes, bytes] = {}
    for line in lines:
        if b":" not in line:
            raise WaczError(f"malformed header in {context}")
        name, value = line.split(b":", 1)
        key = name.strip().lower()
        if not key or key in headers:
            raise WaczError(f"duplicate or empty header in {context}")
        headers[key] = value.strip()
    return headers


def _header_end(data: bytes, start: int) -> tuple[int, int]:
    crlf = data.find(b"\r\n\r\n", start)
    lf = data.find(b"\n\n", start)
    candidates = [(value, length) for value, length in ((crlf, 4), (lf, 2)) if value >= 0]
    if not candidates:
        raise WaczError("record header is not terminated")
    return min(candidates, key=lambda item: item[0])


def _cdx_bytes(records: Sequence[_WarcRecord]) -> bytes:
    lines = []
    for record in records:
        value = {
            "digest": record.digest,
            "filename": record.filename,
            "length": record.length,
            "mime": record.mime,
            "offset": record.offset,
            "status": record.status,
            "url": record.url,
        }
        lines.append(
            f"{_surt(record.url)} {_cdx_timestamp(record.timestamp)} "
            f"{json.dumps(value, separators=(',', ':'), sort_keys=True)}\n"
        )
    return "".join(lines).encode("utf-8")


def _pages(records: Sequence[_WarcRecord]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    pages: list[dict[str, Any]] = []
    for record in records:
        key = (record.url, record.timestamp)
        if key in seen:
            continue
        seen.add(key)
        identifier = hashlib.sha256(f"{record.url}\n{record.timestamp}".encode()).hexdigest()[:16]
        pages.append({"id": identifier, "url": record.url, "ts": record.timestamp})
    return pages


def _pages_bytes(pages: Sequence[Mapping[str, Any]]) -> bytes:
    lines = [json.dumps({"format": "json-pages-1.0", "id": "pages", "title": "All Pages"}, separators=(",", ":"), sort_keys=True)]
    lines.extend(json.dumps(page, separators=(",", ":"), sort_keys=True) for page in pages)
    return ("\n".join(lines) + "\n").encode("utf-8")


def _validate_pages(data: bytes, errors: list[str]) -> int:
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeError as exc:
        errors.append(f"pages/pages.jsonl is not UTF-8: {exc}")
        return 0
    if not lines:
        errors.append("pages/pages.jsonl is empty")
        return 0
    count = 0
    for index, line in enumerate(lines):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"invalid pages JSON on line {index + 1}: {exc}")
            continue
        if index == 0:
            if not isinstance(value, dict) or value.get("format") != "json-pages-1.0":
                errors.append("pages header must declare json-pages-1.0")
            continue
        if not isinstance(value, dict) or not isinstance(value.get("url"), str) or not isinstance(value.get("ts"), str):
            errors.append(f"page line {index + 1} requires string url and ts")
            continue
        try:
            _url(value["url"])
            _rfc3339(value["ts"])
            count += 1
        except WaczError as exc:
            errors.append(f"invalid page line {index + 1}: {exc}")
    return count


def _validate_cdx(
    package: zipfile.ZipFile,
    data: bytes,
    members: Mapping[str, zipfile.ZipInfo],
    unsafe_members: set[str],
    errors: list[str],
) -> int:
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeError as exc:
        errors.append(f"CDXJ is not UTF-8: {exc}")
        return 0
    previous: tuple[str, str] | None = None
    count = 0
    for index, line in enumerate(lines, 1):
        parts = line.split(" ", 2)
        if len(parts) != 3 or not re.fullmatch(r"[0-9]{14}", parts[1]):
            errors.append(f"malformed CDXJ line {index}")
            continue
        key = (parts[0], parts[1])
        if previous is not None and key < previous:
            errors.append("CDXJ records are not sorted")
        previous = key
        try:
            value = json.loads(parts[2])
        except json.JSONDecodeError as exc:
            errors.append(f"invalid CDXJ JSON on line {index}: {exc}")
            continue
        if not isinstance(value, dict):
            errors.append(f"CDXJ value on line {index} must be an object")
            continue
        filename = value.get("filename")
        offset = value.get("offset")
        length = value.get("length")
        archive_path = f"archive/{filename}" if isinstance(filename, str) else ""
        info = members.get(archive_path)
        if (
            info is None
            or type(offset) is not int
            or type(length) is not int
            or offset < 0
            or length <= 0
            or offset + length > info.file_size
        ):
            errors.append(f"CDXJ line {index} has an invalid WARC location")
            continue
        if archive_path in unsafe_members:
            errors.append(f"CDXJ line {index} references an unsafe compressed WARC member")
            continue
        url = value.get("url")
        try:
            if not isinstance(url, str):
                raise WaczError("URL must be a string")
            _url(url)
        except (TypeError, WaczError) as exc:
            errors.append(f"CDXJ line {index} has an invalid URL: {exc}")
            continue
        try:
            record = _indexed_record(package, info, offset, length)
        except (OSError, EOFError, zlib.error, WaczError) as exc:
            errors.append(f"CDXJ line {index} does not reference a valid WARC record: {exc}")
            continue
        expected = {
            "digest": record.digest,
            "filename": record.filename,
            "length": record.length,
            "mime": record.mime,
            "offset": record.offset,
            "status": record.status,
            "url": record.url,
        }
        if parts[0] != _surt(record.url) or parts[1] != _cdx_timestamp(record.timestamp):
            errors.append(f"CDXJ line {index} key does not match its WARC record")
            continue
        if any(value.get(field) != wanted for field, wanted in expected.items()):
            errors.append(f"CDXJ line {index} metadata does not match its WARC record")
            continue
        count += 1
    return count


def _indexed_record(
    package: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    offset: int,
    length: int,
) -> _WarcRecord:
    if length > _WARC_RECORD_LIMIT:
        raise WaczError("indexed WARC record exceeds validation limit")
    with package.open(info) as stream:
        stream.seek(offset)
        raw = stream.read(length)
    if len(raw) != length:
        raise WaczError("indexed WARC record is truncated")
    if info.filename.lower().endswith(".gz"):
        data = _gzip_decompress_bounded(raw, _WARC_RECORD_LIMIT, "indexed WARC record")
        records = _parse_warc_data(
            data,
            info.filename.removeprefix("archive/"),
            offset,
            length,
            max_records=2,
        )
    else:
        records = _parse_warc_data(
            raw,
            info.filename.removeprefix("archive/"),
            offset,
            length,
            max_records=2,
        )
    if len(records) != 1:
        raise WaczError("indexed range does not contain exactly one replayable WARC record")
    return records[0]


def _gzip_decompress_bounded(data: bytes, limit: int, context: str) -> bytes:
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    value = decompressor.decompress(data, limit + 1)
    if len(value) > limit:
        raise WaczError(f"decompressed {context} exceeds validation limit")
    if not decompressor.eof or decompressor.unused_data:
        raise WaczError(f"{context} is not exactly one complete gzip member")
    return value


def _zip_json(
    package: zipfile.ZipFile,
    info: zipfile.ZipInfo | None,
    errors: list[str],
) -> Any:
    if info is None:
        return None
    try:
        return json.loads(_zip_read(package, info, _JSON_LIMIT).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, WaczError) as exc:
        errors.append(f"invalid {info.filename}: {exc}")
        return None


def _zip_read(package: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> bytes:
    if info.file_size > limit:
        raise WaczError(f"ZIP member exceeds validation limit: {info.filename}")
    with package.open(info) as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise WaczError(f"ZIP member exceeds validation limit: {info.filename}")
    return data


def _zip_hash(package: zipfile.ZipFile, info: zipfile.ZipInfo) -> str:
    digest = hashlib.sha256()
    with package.open(info) as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _resource(path: Path, archive_path: str) -> dict[str, Any]:
    return {
        "name": PurePosixPath(archive_path).name,
        "path": archive_path,
        "hash": f"sha256:{_hash_file(path)}",
        "bytes": path.stat().st_size,
    }


def _bytes_resource(data: bytes, archive_path: str) -> dict[str, Any]:
    return {
        "name": PurePosixPath(archive_path).name,
        "path": archive_path,
        "hash": f"sha256:{hashlib.sha256(data).hexdigest()}",
        "bytes": len(data),
    }


def _archive_names(paths: Sequence[str]) -> tuple[str, ...]:
    names = tuple(PurePosixPath(path).name for path in paths)
    if len(names) != len(set(names)):
        raise WaczError("selected WARC files must have unique base names")
    return names


def _write_zip_bytes(
    package: zipfile.ZipFile,
    name: str,
    value: bytes,
    compression: int,
) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = compression
    info.external_attr = 0o100644 << 16
    package.writestr(info, value)


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _snapshot(path: Path) -> tuple[int, int, str]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise WaczError(f"source WARC is not a regular file: {path}")
    return info.st_size, info.st_mtime_ns, _hash_file(path)


def _copy_verified_warc(source: Path, destination: Path, expected_sha256: str) -> None:
    info = source.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise WaczError(f"source WARC is not a regular file: {source}")
    descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    digest = hashlib.sha256()
    try:
        with os.fdopen(descriptor, "rb") as input_stream, destination.open("xb") as output:
            opened = os.fstat(input_stream.fileno())
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise WaczError(f"source WARC changed while opening: {source}")
            for block in iter(lambda: input_stream.read(1024 * 1024), b""):
                output.write(block)
                digest.update(block)
            final = os.fstat(input_stream.fileno())
    except OSError as exc:
        raise WaczError(f"cannot stage source WARC {source}: {exc}") from exc
    if (
        digest.hexdigest() != expected_sha256
        or (opened.st_size, opened.st_mtime_ns) != (final.st_size, final.st_mtime_ns)
    ):
        raise WaczError(f"source WARC changed while staging: {source}")


def _read_regular(path: Path) -> bytes:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise WaczError(f"expected a regular file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise WaczError(f"file changed while opening: {path}")
        return stream.read()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _surt(url: str) -> str:
    parsed = urlsplit(_url(url))
    host = parsed.hostname or ""
    labels = host.lower().split(".")
    surt_host = ",".join(reversed(labels))
    if parsed.port is not None:
        surt_host += f":{parsed.port}"
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    return f"{surt_host}){path}"


def _url(value: str) -> str:
    if not isinstance(value, str):
        raise WaczError("URL must be a string")
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise WaczError(f"unsupported or relative URL: {value!r}")
    return value


def _rfc3339(value: str) -> str:
    if not isinstance(value, str):
        raise WaczError("timestamp must be a string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WaczError(f"invalid RFC3339 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise WaczError(f"RFC3339 timestamp lacks timezone: {value!r}")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _cdx_timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(_rfc3339(value).replace("Z", "+00:00"))
    return parsed.strftime("%Y%m%d%H%M%S")


def _validate_metadata(metadata: WaczMetadata) -> None:
    if not metadata.title or not metadata.description:
        raise WaczError("WACZ metadata requires non-empty title and description")
    _rfc3339(metadata.created)
    if metadata.main_page_url is not None:
        _url(metadata.main_page_url)
    if metadata.main_page_date is not None:
        _rfc3339(metadata.main_page_date)


def _safe_capture_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value or value != path.as_posix() or path.is_absolute() or ".." in path.parts or "\\" in value:
        raise WaczError(f"unsafe capture path: {value!r}")
    return value


def _safe_zip_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or value != path.as_posix()
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or value.endswith("/")
    ):
        raise WaczError(f"unsafe ZIP member path: {value!r}")
    return value


def _is_warc_name(value: str) -> bool:
    lower = value.lower()
    return lower.endswith(".warc") or lower.endswith(".warc.gz")


def _profile(value: ExportProfile | str) -> ExportProfile:
    try:
        return ExportProfile(value)
    except ValueError as exc:
        raise WaczError(f"unsupported export profile: {value!r}") from exc


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
