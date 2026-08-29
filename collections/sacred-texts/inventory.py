"""Validate the archive-first Internet Sacred Text Archive capture."""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
from typing import Sequence


ITEM_IDENTIFIER = "sacred_texts_com_2021_10_09"
METADATA_FILENAME = ITEM_IDENTIFIER
WARC_FILENAME = "sappho.warc.gz"
WARC_CDX_FILENAME = "sappho.warc.os.cdx.gz"
EXPECTED_IA_ARTIFACTS = {
    "sacred_texts_com_2021_10_09.cdx.gz": (
        6494324,
        "5e2ebcbe9998498e2fa4e325ef53e6218d3ee92f",
    ),
    "sacred_texts_com_2021_10_09.cdx.idx": (
        5361,
        "f6214cebd55bb5b80d1a02767b8a6c55f42d7c93",
    ),
    "sacred_texts_com_2021_10_09_archive.torrent": (
        28013,
        "971ac8896caf200e4becfeefbf600812f5ae05bf",
    ),
    "sacred_texts_com_2021_10_09_files.xml": (None, None),
    "sacred_texts_com_2021_10_09_meta.sqlite": (
        20480,
        "ba1d6d694f9d429b4e5b3459f67e537115dda605",
    ),
    "sacred_texts_com_2021_10_09_meta.xml": (
        817,
        "edd557b1f688557690cd18626a39bd64d2c0bdac",
    ),
    WARC_FILENAME: (
        1334243087,
        "98b1773b1eacafc0ed6a2a0d003d87ff5cbaa6e4",
    ),
    WARC_CDX_FILENAME: (
        6746288,
        "5db7613c662795e6935ecbc4ce98fd14eb275ac7",
    ),
}
EXPECTED_CDX_RECORD_COUNT = 154_080
PUBLISHER_MEDIA_VERSION = "9.0"
PUBLISHER_MEDIA_LOGICAL_BYTES = 2_988_233_761
PUBLISHER_MEDIA_FILE_COUNT = 173_566
PUBLISHER_MEDIA_DIRECTORY_COUNT = 2_884
DOWNLOAD_PAGE_PAYLOAD_COUNT = 358
WARC_DOWNLOAD_PAGE_SUCCESS_COUNT = 348
RECOVERED_DOWNLOADS = {
    "bot.txt.gz": (
        137_917,
        "b167c92ed350cea6f54d291d2c2970505fe581c0cbf45fa54eabda2fb0b0a22b",
        386_099,
        "95b32c3196aefa2e594305b4950baa5b0b223e6fffce7d82024d4af781d62f16",
    ),
    "mosy.txt.gz": (
        182_049,
        "0dd5d453e17979170078abb50571ec2815874c8ecc570d9af57bc5082301995e",
        499_890,
        "aeb2eaa75cacaf46235532233655e8ef85f4072cce5c0425086811c98487014f",
    ),
    "sym.txt.gz": (
        94_788,
        "f6a3390371a47298f776e587902b92caa60fe2c476c87e452ba8926f53f75a8e",
        266_751,
        "64f956da4f7c4c69607c2babf6d05f922fcc07b4a486fb97902417d3448d94a8",
    ),
}
MAX_CDX_RECORDS = 1_000_000
MAX_CDX_LINE_BYTES = 1024 * 1024
MAX_RECOVERED_TEXT_BYTES = 10 * 1024 * 1024


class InventoryError(ValueError):
    """Raised when an archival snapshot is unsafe or structurally ambiguous."""


def analyze_capture(
    capture_directory: Path,
    *,
    expected_work_count: int,
    required_representation_kinds: Sequence[str],
    required_source_ids: Sequence[str],
) -> dict[str, object]:
    """Validate the fixed Internet Archive item without extracting its WARC."""
    capture = _read_json(capture_directory / "capture.json")
    source_results = {
        item.get("source_id"): item
        for item in capture.get("sources", [])
        if isinstance(item, dict) and isinstance(item.get("source_id"), str)
    }
    errors = [
        f"required source {source_id!r} is not complete"
        for source_id in required_source_ids
        if source_results.get(source_id, {}).get("status")
        not in {"complete", "complete_with_warnings"}
    ]
    archive_source_present = "internet-archive-2021" in source_results
    files: dict[str, Path] = {}
    if archive_source_present:
        mirror = capture_directory / "sources/internet-archive-2021/mirror"
        files, file_errors = _find_capture_files(mirror)
        errors.extend(file_errors)

    metadata: dict[str, object] | None = None
    metadata_path = files.get(METADATA_FILENAME)
    if metadata_path is not None:
        try:
            metadata = _read_json(metadata_path)
            if metadata.get("metadata", {}).get("identifier") != ITEM_IDENTIFIER:
                errors.append("Internet Archive metadata identifier does not match")
            errors.extend(_validate_metadata_files(metadata))
        except (OSError, UnicodeError, json.JSONDecodeError, InventoryError) as exc:
            errors.append(f"cannot validate Internet Archive metadata: {exc}")

    artifact_reports: list[dict[str, object]] = []
    for name, (expected_size, expected_sha1) in EXPECTED_IA_ARTIFACTS.items():
        path = files.get(name)
        if path is None:
            continue
        size = path.stat().st_size
        sha1 = _digest(path, "sha1") if expected_sha1 is not None else None
        if expected_size is not None and size != expected_size:
            errors.append(f"artifact size mismatch for {name}: {size} != {expected_size}")
        if expected_sha1 is not None and sha1 != expected_sha1:
            errors.append(f"artifact SHA-1 mismatch for {name}")
        artifact_reports.append({"name": name, "size": size, "sha1": sha1})

    cdx_record_count = 0
    cdx_path = files.get(WARC_CDX_FILENAME)
    if cdx_path is not None:
        try:
            cdx_record_count = _count_cdx_records(cdx_path)
        except (OSError, UnicodeError, gzip.BadGzipFile, InventoryError) as exc:
            errors.append(f"cannot inspect WARC CDX: {exc}")
    if (
        archive_source_present
        and "warc-record" in required_representation_kinds
        and cdx_record_count != EXPECTED_CDX_RECORD_COUNT
    ):
        errors.append(
            f"WARC CDX has {cdx_record_count} records, "
            f"expected {EXPECTED_CDX_RECORD_COUNT}"
        )

    warc_path = files.get(WARC_FILENAME)
    if warc_path is not None:
        try:
            with gzip.open(warc_path, "rb") as stream:
                if not stream.read(16).startswith(b"WARC/"):
                    errors.append("WARC payload does not begin with a WARC version record")
        except (OSError, gzip.BadGzipFile) as exc:
            errors.append(f"cannot open compressed WARC: {exc}")

    recovered_downloads, recovery_errors = _validate_download_recovery(
        capture_directory,
        source_results,
    )
    errors.extend(recovery_errors)

    errors.append(
        "official ISTA DVD-ROM/USB 9.0 distribution is not preserved; its published "
        "inventory contains 173566 files in 2884 directories"
    )
    warnings = [
        "the 2021 third-party WARC is an independently useful historical baseline, not a complete ISTA corpus",
        "live comprehensive capture requires ISTA permission and Cloudflare allowlisting",
    ]
    return {
        "schema_version": 1,
        "capture_id": capture.get("capture_id"),
        "status": "incomplete" if errors else "complete_with_warnings",
        "errors": errors,
        "warnings": warnings,
        "required_sources": list(required_source_ids),
        "source_statuses": {
            source_id: source_results.get(source_id, {}).get("status", "missing")
            for source_id in required_source_ids
        },
        "item_identifier": ITEM_IDENTIFIER,
        "artifact_count": len(artifact_reports),
        "artifacts": artifact_reports,
        "cdx_record_count": cdx_record_count,
        "expected_cdx_record_count": EXPECTED_CDX_RECORD_COUNT,
        "expected_work_count": expected_work_count,
        "publisher_media": {
            "version": PUBLISHER_MEDIA_VERSION,
            "preserved": False,
            "published_logical_bytes": PUBLISHER_MEDIA_LOGICAL_BYTES,
            "published_file_count": PUBLISHER_MEDIA_FILE_COUNT,
            "published_directory_count": PUBLISHER_MEDIA_DIRECTORY_COUNT,
        },
        "download_page": {
            "published_payload_count": DOWNLOAD_PAGE_PAYLOAD_COUNT,
            "warc_successful_payload_count": WARC_DOWNLOAD_PAGE_SUCCESS_COUNT,
            "recovered_in_capture_count": len(recovered_downloads),
            "remaining_known_gap_count": (
                DOWNLOAD_PAGE_PAYLOAD_COUNT
                - WARC_DOWNLOAD_PAGE_SUCCESS_COUNT
                - len(recovered_downloads)
            ),
            "recovered_payloads": recovered_downloads,
        },
        "metadata_file_count": (
            len(metadata.get("files", [])) if isinstance(metadata, dict) else 0
        ),
    }


def _validate_download_recovery(
    capture_directory: Path,
    source_results: dict[str | None, dict[str, object]],
) -> tuple[list[dict[str, object]], list[str]]:
    source_id = "wayback-download-recovery"
    if source_id not in source_results:
        return [], []
    root = capture_directory / f"sources/{source_id}/mirror"
    matches = {
        name: [path for path in root.rglob(name) if path.is_file() and not path.is_symlink()]
        if root.is_dir()
        else []
        for name in RECOVERED_DOWNLOADS
    }
    reports: list[dict[str, object]] = []
    errors: list[str] = []
    for name, expected in RECOVERED_DOWNLOADS.items():
        paths = matches[name]
        if len(paths) != 1:
            errors.append(
                f"recovered download {name} was found {len(paths)} times, expected once"
            )
            continue
        path = paths[0]
        size, sha256, text_size, text_sha256 = expected
        if path.stat().st_size != size or _digest(path, "sha256") != sha256:
            errors.append(f"recovered download fixity mismatch for {name}")
            continue
        try:
            actual_text_size, actual_text_sha256 = _digest_gzip_payload(path)
        except (OSError, gzip.BadGzipFile, InventoryError) as exc:
            errors.append(f"cannot inspect recovered download {name}: {exc}")
            continue
        if actual_text_size != text_size or actual_text_sha256 != text_sha256:
            errors.append(f"recovered text payload fixity mismatch for {name}")
            continue
        reports.append(
            {
                "name": name,
                "size": size,
                "sha256": sha256,
                "text_size": text_size,
                "text_sha256": text_sha256,
            }
        )
    return reports, errors


def _find_capture_files(root: Path) -> tuple[dict[str, Path], list[str]]:
    expected = set(EXPECTED_IA_ARTIFACTS) | {METADATA_FILENAME}
    matches: dict[str, list[Path]] = {name: [] for name in expected}
    if root.is_dir():
        for path in root.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            if path.name in matches:
                matches[path.name].append(path)
    files = {name: paths[0] for name, paths in matches.items() if len(paths) == 1}
    errors: list[str] = []
    for name in sorted(matches):
        if not matches[name]:
            errors.append(f"captured Internet Archive object was not found: {name}")
        elif len(matches[name]) > 1:
            errors.append(f"multiple captured Internet Archive objects found: {name}")
    return files, errors


def _validate_metadata_files(metadata: dict[str, object]) -> list[str]:
    raw_files = metadata.get("files")
    if not isinstance(raw_files, list):
        raise InventoryError("metadata files list is missing")
    indexed = {
        item.get("name"): item
        for item in raw_files
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    errors: list[str] = []
    for name, (expected_size, expected_sha1) in EXPECTED_IA_ARTIFACTS.items():
        item = indexed.get(name)
        if item is None:
            errors.append(f"Internet Archive metadata omits artifact: {name}")
            continue
        if expected_size is not None and item.get("size") != str(expected_size):
            errors.append(f"Internet Archive metadata size mismatch for {name}")
        if expected_sha1 is not None and item.get("sha1") != expected_sha1:
            errors.append(f"Internet Archive metadata SHA-1 mismatch for {name}")
    return errors


def _count_cdx_records(path: Path) -> int:
    count = 0
    with gzip.open(path, "rb") as stream:
        for line_number, line in enumerate(stream, 1):
            if len(line) > MAX_CDX_LINE_BYTES:
                raise InventoryError(f"CDX line {line_number} exceeds safety limit")
            stripped = line.strip()
            if not stripped or stripped.startswith(b"CDX ") or stripped.startswith(b" CDX "):
                continue
            count += 1
            if count > MAX_CDX_RECORDS:
                raise InventoryError("CDX record count exceeds safety limit")
    return count


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise InventoryError(f"JSON document must contain an object: {path}")
    return value


def _digest(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _digest_gzip_payload(path: Path) -> tuple[int, str]:
    size = 0
    digest = hashlib.sha256()
    with gzip.open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            if size > MAX_RECOVERED_TEXT_BYTES:
                raise InventoryError("recovered text payload exceeds safety limit")
            digest.update(chunk)
    return size, digest.hexdigest()
