"""Run collection-specific preservation validation outside archive masters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping, Sequence

from text_preserver.adapters import _adapter_path, _load_adapter
from text_preserver.preservation.capture.plan import CAPTURE_ID_RE
from text_preserver.config import SAFE_ID_RE, CollectionConfig, Config
from text_preserver.derived import (
    AnalysisError,
    _ensure_derived_directory,
    _find_collection,
    _fsync_directory,
    _read_json,
    _sha256,
)
from text_preserver.preservation.fixity import MANIFEST_NAME, verify_capture


@dataclass(frozen=True)
class AnalysisResult:
    capture_directory: Path
    report_path: Path
    report: Mapping[str, Any]
    contributing_capture_directories: tuple[Path, ...] = ()
    contributing_capture_ids: tuple[str, ...] = ()

    @property
    def status(self) -> str:
        return str(self.report["status"])

    def to_dict(self) -> dict[str, Any]:
        return {
            "capture_directory": str(self.capture_directory),
            "contributing_capture_directories": [
                str(path) for path in self.contributing_capture_directories
            ],
            "contributing_capture_ids": list(self.contributing_capture_ids),
            "report_path": str(self.report_path),
            "report": dict(self.report),
        }


VALIDATION_REPORT_SCHEMA_VERSION = 3
SUCCESS_STATUSES = frozenset({"complete", "complete_with_warnings"})
SOURCE_STATUSES = SUCCESS_STATUSES | frozenset(
    {"running", "partial", "failed", "interrupted"}
)


@dataclass(frozen=True)
class _AnalysisCapture:
    directory: Path
    capture_id: str
    manifest_sha256: str
    source_records: Mapping[str, Mapping[str, Any]]
    source_directories: Mapping[str, Path]


def analyze_preservation(
    config: Config,
    collection_id: str,
    capture_path: str | Path | Sequence[str | Path] | None = None,
) -> AnalysisResult:
    """Verify and analyze one or more captures into an immutable validation."""
    collection = _find_collection(config, collection_id)
    requested_paths = _analysis_capture_paths(
        config,
        collection,
        capture_path,
    )
    captures: list[_AnalysisCapture] = []
    by_directory: dict[Path, _AnalysisCapture] = {}
    by_id: dict[str, Path] = {}
    for requested_path, pointer_source_id in requested_paths:
        capture = _verified_analysis_capture(requested_path, collection.id)
        if pointer_source_id is not None:
            source_record = capture.source_records.get(pointer_source_id)
            if source_record is None or source_record.get("status") not in SUCCESS_STATUSES:
                raise AnalysisError(
                    f"capture pointer LATEST-{pointer_source_id} does not identify a "
                    f"successful matching source"
                )
        existing = by_directory.get(capture.directory)
        if existing is not None:
            continue
        other_directory = by_id.get(capture.capture_id)
        if other_directory is not None and other_directory != capture.directory:
            raise AnalysisError(
                f"capture ID {capture.capture_id!r} is provided by multiple directories"
            )
        by_directory[capture.directory] = capture
        by_id[capture.capture_id] = capture.directory
        captures.append(capture)

    selected_sources = _select_source_captures(captures)
    contributing_by_id = {
        capture.capture_id: capture for capture, _record, _directory in selected_sources.values()
    }
    contributing = tuple(
        contributing_by_id[capture_id] for capture_id in sorted(contributing_by_id)
    )
    primary = max(contributing, key=lambda value: value.capture_id)
    capture_directory = primary.directory
    capture_id = primary.capture_id

    adapter_value = collection.analysis.get("inventory_adapter")
    if not isinstance(adapter_value, str):
        raise AnalysisError(f"collection {collection.id} has no inventory adapter")
    aggregate = len(contributing) > 1
    adapter_path, adapter_source, recipe_bundle_identity = _adapter_path(
        config,
        collection,
        capture_directory,
        adapter_value,
        prefer_preserved=(
            False
            if aggregate
            else collection.analysis.get("prefer_preserved_adapter", True)
        ),
    )
    adapter, adapter_bytes = _load_adapter(adapter_path)
    analyze = getattr(adapter, "analyze_capture", None)
    if not callable(analyze):
        raise AnalysisError(f"inventory adapter does not export analyze_capture(): {adapter_path}")

    expected_work_count = collection.analysis.get("expected_work_count", 0)
    required_representation_kinds = tuple(
        collection.analysis.get("required_representation_kinds", ())
    )
    required_source_ids = tuple(source.id for source in collection.sources if source.required)
    configuration_sha256 = hashlib.sha256(config.input_bytes).hexdigest()
    source_capture_map = {
        source_id: capture.capture_id
        for source_id, (capture, _record, _directory) in sorted(selected_sources.items())
    }
    validation_inputs: dict[str, Any] = {
        "schema_version": VALIDATION_REPORT_SCHEMA_VERSION,
        "captures": [
            {
                "capture_id": capture.capture_id,
                "manifest_sha256": capture.manifest_sha256,
            }
            for capture in contributing
        ],
        "source_capture_map": source_capture_map,
        "analyzer": {
            "source": adapter_source,
            "sha256": hashlib.sha256(adapter_bytes).hexdigest(),
        },
        "expected_work_count": expected_work_count,
        "required_representation_kinds": list(required_representation_kinds),
        "required_source_ids": list(required_source_ids),
        "configuration_sha256": configuration_sha256,
        "recipe_bundle": recipe_bundle_identity,
    }
    validation_id = hashlib.sha256(_canonical_json(validation_inputs)).hexdigest()
    relative_validation = (
        Path("collections") / collection.id / "validations" / validation_id
    )
    validation_directory = _ensure_derived_directory(
        config.project.derived_root,
        relative_validation,
    )
    report_path = validation_directory / "report.json"

    if report_path.exists() or report_path.is_symlink():
        report = _read_validation_report(
            report_path,
            collection.id,
            validation_id,
            validation_inputs,
        )
        _verify_analysis_captures(contributing, "capture changed during analysis")
        _update_validation_pointers(
            config.project.derived_root,
            collection.id,
            validation_id,
            str(report["status"]),
        )
        return AnalysisResult(
            capture_directory,
            report_path,
            report,
            tuple(capture.directory for capture in contributing),
            tuple(capture.capture_id for capture in contributing),
        )

    try:
        if aggregate:
            with tempfile.TemporaryDirectory(prefix="text-preserver-validation-") as temporary:
                adapter_capture = _build_aggregate_capture(
                    Path(temporary),
                    collection.id,
                    primary,
                    selected_sources,
                )
                report = analyze(
                    adapter_capture,
                    expected_work_count=expected_work_count,
                    required_representation_kinds=required_representation_kinds,
                    required_source_ids=required_source_ids,
                )
        else:
            report = analyze(
                capture_directory,
                expected_work_count=expected_work_count,
                required_representation_kinds=required_representation_kinds,
                required_source_ids=required_source_ids,
            )
    except Exception as exc:
        raise AnalysisError(f"inventory adapter failed {adapter_path}: {exc}") from exc
    if not isinstance(report, dict) or report.get("status") not in {
        "complete",
        "complete_with_warnings",
        "incomplete",
    }:
        raise AnalysisError(f"inventory adapter returned an invalid report: {adapter_path}")
    for key, expected in (("collection_id", collection.id), ("capture_id", capture_id)):
        if key in report and report[key] != expected:
            raise AnalysisError(
                f"inventory adapter returned {key} {report[key]!r}, expected {expected!r}"
            )
        report[key] = expected
    report["schema_version"] = VALIDATION_REPORT_SCHEMA_VERSION
    report["validation_id"] = validation_id
    report["created_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    report["validation_inputs"] = validation_inputs
    report["recipe_bundle"] = recipe_bundle_identity
    report["source_capture_map"] = source_capture_map
    report["contributing_capture_ids"] = [
        capture.capture_id for capture in contributing
    ]
    report["contributing_capture_directories"] = [
        str(capture.directory) for capture in contributing
    ]
    report["analyzer"] = {
        "path": str(adapter_path),
        "source": adapter_source,
        "sha256": hashlib.sha256(adapter_bytes).hexdigest(),
    }
    if adapter_source != "preserved_capture":
        warnings = report.setdefault("warnings", [])
        if not isinstance(warnings, list):
            raise AnalysisError("inventory adapter returned invalid warnings")
        warnings.append(
            "analysis used the current recipe adapter; result may differ from the capture-time assessment"
        )
        if report["status"] == "complete":
            report["status"] = "complete_with_warnings"

    _verify_analysis_captures(contributing, "capture changed during analysis")

    try:
        if not _write_json_once(report_path, report):
            report = _read_validation_report(
                report_path,
                collection.id,
                validation_id,
                validation_inputs,
            )
        _update_validation_pointers(
            config.project.derived_root,
            collection.id,
            validation_id,
            str(report["status"]),
        )
    except (OSError, TypeError, ValueError) as exc:
        raise AnalysisError(f"cannot write validation report {report_path}: {exc}") from exc
    return AnalysisResult(
        capture_directory,
        report_path,
        report,
        tuple(capture.directory for capture in contributing),
        tuple(capture.capture_id for capture in contributing),
    )


def _analysis_capture_paths(
    config: Config,
    collection: CollectionConfig,
    capture_path: str | Path | Sequence[str | Path] | None,
) -> tuple[tuple[Path, str | None], ...]:
    if capture_path is not None:
        if isinstance(capture_path, (str, Path)):
            values: Sequence[str | Path] = (capture_path,)
        elif isinstance(capture_path, Sequence):
            values = capture_path
        else:
            raise AnalysisError("capture paths must be paths or a sequence of paths")
        if not values:
            raise AnalysisError("explicit capture path set must not be empty")
        try:
            return tuple((Path(value), None) for value in values)
        except TypeError as exc:
            raise AnalysisError("capture paths must contain only filesystem paths") from exc

    collection_root = config.project.archive_root / "collections" / collection.id
    if collection_root.is_symlink() or not collection_root.is_dir():
        raise AnalysisError(f"collection capture directory is unavailable: {collection_root}")
    canonical = collection_root / "LATEST-ACQUIRED"
    legacy = collection_root / "LATEST"
    if canonical.is_symlink() or canonical.exists():
        pointer_names = [canonical.name]
    elif legacy.is_symlink() or legacy.exists():
        pointer_names = [legacy.name]
    else:
        pointer_names = []
    pointer_names.extend(
        pointer.name
        for source in collection.sources
        if (pointer := collection_root / f"LATEST-{source.id}").is_symlink()
        or pointer.exists()
    )
    if not pointer_names:
        raise AnalysisError(f"collection {collection.id} has no capture pointers")
    return tuple(
        (
            _read_analysis_capture_pointer(collection_root, name),
            name.removeprefix("LATEST-")
            if name not in {"LATEST", "LATEST-ACQUIRED"}
            else None,
        )
        for name in pointer_names
    )


def _read_analysis_capture_pointer(collection_root: Path, name: str) -> Path:
    pointer = collection_root / name
    if pointer.is_symlink() or not pointer.is_file():
        raise AnalysisError(f"capture pointer is unavailable: {pointer}")
    try:
        value = pointer.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise AnalysisError(f"cannot read capture pointer {pointer}: {exc}") from exc
    relative = Path(value)
    if (
        len(relative.parts) != 2
        or relative.parts[0] != "captures"
        or not _safe_capture_id(relative.parts[1])
    ):
        raise AnalysisError(f"unsafe capture pointer content: {pointer}")
    target = collection_root / relative
    if target.is_symlink():
        raise AnalysisError(f"capture pointer targets a symlink: {pointer}")
    resolved = target.resolve()
    if not resolved.is_relative_to(collection_root.resolve()):
        raise AnalysisError(f"capture pointer escapes collection directory: {pointer}")
    if resolved.name != relative.parts[1]:
        raise AnalysisError(f"capture pointer has a mismatched capture ID: {pointer}")
    return resolved


def _verified_analysis_capture(path: Path, collection_id: str) -> _AnalysisCapture:
    verification = verify_capture(path)
    if not verification.ok:
        raise AnalysisError(
            "capture fixity verification failed: " + "; ".join(verification.errors)
        )
    capture_directory = verification.capture_directory
    capture_metadata = _read_json(capture_directory / "capture.json", "capture metadata")
    if capture_metadata.get("collection_id") != collection_id:
        raise AnalysisError(
            f"capture belongs to collection {capture_metadata.get('collection_id')!r}, "
            f"not {collection_id!r}"
        )
    capture_id = capture_metadata.get("capture_id")
    if not isinstance(capture_id, str) or not _safe_capture_id(capture_id):
        raise AnalysisError(f"capture metadata has an unsafe capture ID: {capture_id!r}")
    if capture_id != capture_directory.name:
        raise AnalysisError(
            f"capture metadata ID {capture_id!r} does not match directory "
            f"{capture_directory.name!r}"
        )

    raw_sources = capture_metadata.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise AnalysisError(f"capture {capture_id} has no safe source metadata")
    source_records: dict[str, Mapping[str, Any]] = {}
    source_directories: dict[str, Path] = {}
    sources_root = capture_directory / "sources"
    if sources_root.is_symlink() or not sources_root.is_dir():
        raise AnalysisError(f"capture {capture_id} has no safe sources directory")
    resolved_capture = capture_directory.resolve()
    for index, record in enumerate(raw_sources):
        if not isinstance(record, dict):
            raise AnalysisError(f"capture {capture_id} has an unsafe source record at index {index}")
        source_id = record.get("source_id")
        status = record.get("status")
        if not isinstance(source_id, str) or SAFE_ID_RE.fullmatch(source_id) is None:
            raise AnalysisError(
                f"capture {capture_id} has an unsafe source ID at index {index}: {source_id!r}"
            )
        if source_id in source_records:
            raise AnalysisError(f"capture {capture_id} has duplicate source metadata for {source_id!r}")
        if status not in SOURCE_STATUSES:
            raise AnalysisError(
                f"capture {capture_id} has an unsafe status for source {source_id!r}: {status!r}"
            )
        source_directory = sources_root / source_id
        if source_directory.is_symlink() or not source_directory.is_dir():
            raise AnalysisError(
                f"capture {capture_id} has no safe directory for source {source_id!r}"
            )
        resolved_source = source_directory.resolve()
        if not resolved_source.is_relative_to(resolved_capture):
            raise AnalysisError(
                f"source {source_id!r} escapes verified capture {capture_id}"
            )
        source_records[source_id] = record
        source_directories[source_id] = resolved_source
    return _AnalysisCapture(
        capture_directory,
        capture_id,
        _sha256(capture_directory / MANIFEST_NAME),
        source_records,
        source_directories,
    )


def _safe_capture_id(value: str) -> bool:
    match = CAPTURE_ID_RE.fullmatch(value)
    if match is None:
        return False
    try:
        datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ")
    except ValueError:
        return False
    return True


def _select_source_captures(
    captures: Sequence[_AnalysisCapture],
) -> dict[str, tuple[_AnalysisCapture, Mapping[str, Any], Path]]:
    providers: dict[str, list[tuple[_AnalysisCapture, Mapping[str, Any], Path]]] = {}
    for capture in captures:
        for source_id, record in capture.source_records.items():
            providers.setdefault(source_id, []).append(
                (capture, record, capture.source_directories[source_id])
            )
    if not providers:
        raise AnalysisError("requested captures cannot provide source metadata safely")
    return {
        source_id: max(
            values,
            key=lambda value: (
                value[1].get("status") in SUCCESS_STATUSES,
                value[0].capture_id,
            ),
        )
        for source_id, values in sorted(providers.items())
    }


def _build_aggregate_capture(
    root: Path,
    collection_id: str,
    primary: _AnalysisCapture,
    selected_sources: Mapping[
        str, tuple[_AnalysisCapture, Mapping[str, Any], Path]
    ],
) -> Path:
    sources_root = root / "sources"
    sources_root.mkdir()
    records: list[Mapping[str, Any]] = []
    for source_id, (capture, record, source_directory) in sorted(selected_sources.items()):
        resolved_source = source_directory.resolve()
        if not resolved_source.is_relative_to(capture.directory.resolve()):
            raise AnalysisError(
                f"aggregate source {source_id!r} escapes verified capture {capture.capture_id}"
            )
        (sources_root / source_id).symlink_to(resolved_source, target_is_directory=True)
        records.append(record)
    statuses = [record.get("status") for record in records]
    if all(status == "complete" for status in statuses):
        status = "complete"
    elif all(value in SUCCESS_STATUSES for value in statuses):
        status = "complete_with_warnings"
    else:
        status = "partial"
    metadata = {
        "schema_version": 1,
        "capture_id": primary.capture_id,
        "collection_id": collection_id,
        "status": status,
        "selected_sources": sorted(selected_sources),
        "sources": records,
    }
    (root / "capture.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return root


def _verify_analysis_captures(
    captures: Sequence[_AnalysisCapture],
    label: str,
) -> None:
    errors: list[str] = []
    for capture in captures:
        verification = verify_capture(capture.directory)
        if not verification.ok:
            errors.extend(
                f"{capture.capture_id}: {error}" for error in verification.errors
            )
            continue
        if _sha256(capture.directory / MANIFEST_NAME) != capture.manifest_sha256:
            errors.append(f"{capture.capture_id}: fixity manifest changed")
    if errors:
        raise AnalysisError(label + ": " + "; ".join(errors))


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _write_json_once(path: Path, value: Mapping[str, Any]) -> bool:
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        temporary.unlink()
        _fsync_directory(path.parent)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def _read_validation_report(
    path: Path,
    collection_id: str,
    validation_id: str,
    validation_inputs: Mapping[str, Any],
) -> dict[str, Any]:
    report = _read_json(path, "validation report")
    if report.get("schema_version") != VALIDATION_REPORT_SCHEMA_VERSION:
        raise AnalysisError(f"existing validation report has an unsupported schema: {path}")
    if report.get("collection_id") != collection_id:
        raise AnalysisError(f"existing validation report has a mismatched collection: {path}")
    if report.get("validation_id") != validation_id:
        raise AnalysisError(f"existing validation report has a mismatched ID: {path}")
    if report.get("validation_inputs") != validation_inputs:
        raise AnalysisError(f"existing validation report has mismatched inputs: {path}")
    if report.get("status") not in {
        "complete",
        "complete_with_warnings",
        "incomplete",
    }:
        raise AnalysisError(f"existing validation report has an invalid status: {path}")
    return report


def _update_validation_pointers(
    derived_root: Path,
    collection_id: str,
    validation_id: str,
    status: str,
) -> None:
    collection_root = _ensure_derived_directory(
        derived_root,
        Path("collections") / collection_id,
    )
    validations_root = _ensure_derived_directory(
        derived_root,
        Path("collections") / collection_id / "validations",
    )
    validation_directory = validations_root / validation_id
    if (
        validation_directory.is_symlink()
        or not validation_directory.is_dir()
        or not (validation_directory / "report.json").is_file()
        or (validation_directory / "report.json").is_symlink()
    ):
        raise AnalysisError(f"validation pointer target is unavailable: {validation_directory}")

    _validate_existing_validation_pointer(
        validations_root,
        "LATEST",
        validated_only=False,
    )
    _validate_existing_validation_pointer(
        collection_root,
        "LATEST-VALIDATED",
        validated_only=True,
    )
    _atomic_write_bytes(
        validations_root / "LATEST",
        f"{validation_id}\n".encode("utf-8"),
    )
    _fsync_directory(validations_root)
    if status in SUCCESS_STATUSES:
        _atomic_write_bytes(
            collection_root / "LATEST-VALIDATED",
            f"validations/{validation_id}\n".encode("utf-8"),
        )
        _fsync_directory(collection_root)


def _validate_existing_validation_pointer(
    parent: Path,
    name: str,
    *,
    validated_only: bool,
) -> None:
    pointer = parent / name
    if pointer.is_symlink():
        raise AnalysisError(f"validation pointer must not be a symlink: {pointer}")
    if not pointer.exists():
        return
    if not pointer.is_file():
        raise AnalysisError(f"validation pointer is not a regular file: {pointer}")
    try:
        value = pointer.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise AnalysisError(f"cannot read validation pointer {pointer}: {exc}") from exc
    relative = Path(value)
    if validated_only:
        safe = (
            len(relative.parts) == 2
            and relative.parts[0] == "validations"
            and _valid_validation_id(relative.parts[1])
        )
    else:
        safe = len(relative.parts) == 1 and _valid_validation_id(relative.parts[0])
    if not safe:
        raise AnalysisError(f"unsafe validation pointer content: {pointer}")
    target = parent / relative
    if target.is_symlink() or not target.is_dir():
        raise AnalysisError(f"validation pointer target is unavailable: {pointer}")
    resolved = target.resolve()
    if not resolved.is_relative_to(parent.resolve()):
        raise AnalysisError(f"validation pointer escapes its collection: {pointer}")
    report_path = target / "report.json"
    if report_path.is_symlink() or not report_path.is_file():
        raise AnalysisError(f"validation pointer report is unavailable: {pointer}")
    if validated_only:
        report = _read_json(report_path, "validation pointer report")
        if report.get("status") not in SUCCESS_STATUSES:
            raise AnalysisError(f"validated pointer targets an incomplete report: {pointer}")


def _valid_validation_id(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
