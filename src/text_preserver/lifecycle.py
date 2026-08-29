"""Read independent collection lifecycle dimensions without changing storage."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from text_preserver.access.reader import _validated_current_reader_target
from text_preserver.config import Config
from text_preserver.derived import AnalysisError, _find_collection, _read_json
from text_preserver.preservation.capture.plan import CAPTURE_ID_RE
from text_preserver.preservation.fixity import (
    MANIFEST_NAME,
    is_complete_full_capture,
    verify_capture,
)
from text_preserver.preservation.validation import VALIDATION_REPORT_SCHEMA_VERSION


SUCCESS_STATUSES = {"complete", "complete_with_warnings"}


def collection_lifecycle_status(config: Config, collection_id: str) -> dict[str, Any]:
    """Return the four lifecycle dimensions without running adapters or writing files."""
    collection = _find_collection(config, collection_id)
    archive_collection = _collection_root(
        config.project.archive_root, collection.id, "archive"
    )
    derived_collection = _collection_root(
        config.project.derived_root, collection.id, "derived"
    )
    acquisition, capture = _acquisition(
        archive_collection,
        collection.id,
        tuple(source.id for source in collection.sources),
    )
    return {
        "schema_version": 1,
        "collection_id": collection.id,
        "acquisition": acquisition,
        "fixity": _fixity(capture),
        "validation": _validation(derived_collection, collection.id),
        "access": _access(derived_collection),
    }


def _collection_root(root: Path, collection_id: str, label: str) -> Path | None:
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise AnalysisError(f"unsafe {label} root: {root}")
    collections = root / "collections"
    if collections.is_symlink() or (collections.exists() and not collections.is_dir()):
        raise AnalysisError(f"unsafe {label} collections root: {collections}")
    collection = collections / collection_id
    if collection.is_symlink() or (collection.exists() and not collection.is_dir()):
        raise AnalysisError(f"unsafe {label} collection root: {collection}")
    return collection if collection.is_dir() else None


def _acquisition(
    collection_root: Path | None,
    collection_id: str,
    configured_source_ids: tuple[str, ...],
) -> tuple[dict[str, Any], Path | None]:
    if collection_root is None:
        return {"state": "not_acquired"}, None
    canonical = collection_root / "LATEST-ACQUIRED"
    legacy = collection_root / "LATEST"
    if canonical.is_symlink() or canonical.exists():
        pointer = canonical
    elif legacy.is_symlink() or legacy.exists():
        pointer = legacy
    else:
        return {"state": "not_acquired"}, None
    try:
        capture = _capture_pointer(collection_root, pointer)
        metadata = _read_json(capture / "capture.json", "capture metadata")
        capture_id = metadata.get("capture_id")
        status = metadata.get("status")
        if (
            capture_id != capture.name
            or metadata.get("collection_id") != collection_id
            or not is_complete_full_capture(metadata, configured_source_ids)
        ):
            raise AnalysisError("acquisition pointer target has inconsistent capture metadata")
        return {
            "state": "acquired",
            "capture_id": capture_id,
            "status": status,
            "pointer": pointer.name,
            "path": str(capture),
        }, capture
    except AnalysisError as exc:
        return {
            "state": "invalid",
            "pointer": pointer.name,
            "error": str(exc),
        }, _safe_pointer_target(collection_root, pointer)


def _fixity(capture: Path | None) -> dict[str, Any]:
    if capture is None:
        return {"state": "not_finalized"}
    manifest = capture / MANIFEST_NAME
    if manifest.is_symlink() or not manifest.is_file():
        return {
            "state": "not_finalized",
            "capture_id": capture.name,
            "path": str(capture),
        }
    result = verify_capture(capture)
    value: dict[str, Any] = {
        "state": "valid" if result.ok else "invalid",
        "capture_id": capture.name,
        "path": str(capture),
    }
    if not result.ok:
        value["error"] = "; ".join(result.errors)
    return value


def _validation(collection_root: Path | None, collection_id: str) -> dict[str, Any]:
    if collection_root is None:
        return {"state": "not_run"}
    validations = collection_root / "validations"
    if validations.is_symlink() or (validations.exists() and not validations.is_dir()):
        return {"state": "invalid", "error": f"unsafe validations directory: {validations}"}
    latest_pointer = validations / "LATEST"
    successful_pointer = collection_root / "LATEST-VALIDATED"
    latest_present = latest_pointer.is_symlink() or latest_pointer.exists()
    successful_present = successful_pointer.is_symlink() or successful_pointer.exists()
    if not latest_present and not successful_present:
        return {"state": "not_run"}
    value: dict[str, Any] = {}
    errors: list[str] = []
    if latest_present:
        try:
            value["latest_attempt"] = _validation_pointer(
                validations,
                latest_pointer,
                collection_id=collection_id,
                prefixed=False,
                successful=False,
            )
        except AnalysisError as exc:
            errors.append(str(exc))
    if successful_present:
        try:
            value["latest_successful"] = _validation_pointer(
                collection_root,
                successful_pointer,
                collection_id=collection_id,
                prefixed=True,
                successful=True,
            )
        except AnalysisError as exc:
            errors.append(str(exc))
    if errors:
        value["state"] = "invalid"
        value["error"] = "; ".join(errors)
    elif "latest_attempt" in value:
        value["state"] = value["latest_attempt"]["status"]
    else:
        value["state"] = value["latest_successful"]["status"]
    return value


def _validation_pointer(
    parent: Path,
    pointer: Path,
    *,
    collection_id: str,
    prefixed: bool,
    successful: bool,
) -> dict[str, Any]:
    value = _read_pointer_text(pointer)
    relative = Path(value)
    if prefixed:
        safe = (
            len(relative.parts) == 2
            and relative.parts[0] == "validations"
            and _is_sha256(relative.parts[1])
        )
    else:
        safe = len(relative.parts) == 1 and _is_sha256(relative.parts[0])
    if not safe:
        raise AnalysisError(f"unsafe validation pointer content: {pointer}")
    target = parent / relative
    if target.is_symlink() or not target.is_dir():
        raise AnalysisError(f"validation pointer target is unavailable: {pointer}")
    report_path = target / "report.json"
    report = _read_json(report_path, "validation report")
    status = report.get("status")
    if (
        report.get("schema_version") != VALIDATION_REPORT_SCHEMA_VERSION
        or report.get("collection_id") != collection_id
        or report.get("validation_id") != relative.parts[-1]
        or not isinstance(report.get("validation_inputs"), dict)
        or status not in {"complete", "complete_with_warnings", "incomplete"}
    ):
        raise AnalysisError(f"validation pointer report has invalid status: {pointer}")
    if successful and status not in SUCCESS_STATUSES:
        raise AnalysisError(f"successful validation pointer is incomplete: {pointer}")
    return {
        "validation_id": relative.parts[-1],
        "status": status,
        "pointer": pointer.name,
        "path": str(report_path),
    }


def _access(collection_root: Path | None) -> dict[str, Any]:
    if collection_root is None:
        return {"state": "not_run"}
    canonical = collection_root / "LATEST-READER"
    legacy = collection_root / "reader"
    if not (canonical.is_symlink() or canonical.exists() or legacy.is_symlink() or legacy.exists()):
        return {"state": "not_run"}
    try:
        generation = _validated_current_reader_target(
            collection_root, require_pointer=True
        )
        if generation is None:
            return {"state": "not_run"}
        metadata = _read_json(generation / "metadata.json", "reader metadata")
        status = metadata.get("status")
        if status not in SUCCESS_STATUSES:
            raise AnalysisError("current reader metadata has invalid status")
        return {
            "state": status,
            "capture_id": metadata.get("capture_id"),
            "build_key": metadata.get("build_key"),
            "pointer": "LATEST-READER" if canonical.is_file() else "reader",
            "path": str(generation),
        }
    except AnalysisError as exc:
        return {"state": "invalid", "error": str(exc)}


def _capture_pointer(collection_root: Path, pointer: Path) -> Path:
    value = _read_pointer_text(pointer)
    relative = Path(value)
    if (
        relative.is_absolute()
        or len(relative.parts) != 2
        or relative.parts[0] != "captures"
        or CAPTURE_ID_RE.fullmatch(relative.parts[1]) is None
    ):
        raise AnalysisError(f"unsafe capture pointer content: {pointer}")
    target = collection_root / relative
    if target.is_symlink() or not target.is_dir():
        raise AnalysisError(f"capture pointer target is unavailable: {pointer}")
    resolved = target.resolve()
    if not resolved.is_relative_to(collection_root.resolve()):
        raise AnalysisError(f"capture pointer escapes collection root: {pointer}")
    return resolved


def _safe_pointer_target(collection_root: Path, pointer: Path) -> Path | None:
    try:
        return _capture_pointer(collection_root, pointer)
    except AnalysisError:
        return None


def _read_pointer_text(pointer: Path) -> str:
    if pointer.is_symlink() or not pointer.is_file():
        raise AnalysisError(f"pointer is not a regular file: {pointer}")
    try:
        return pointer.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise AnalysisError(f"cannot read pointer {pointer}: {exc}") from exc


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)
