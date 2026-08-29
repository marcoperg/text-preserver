"""Run collection-specific preservation analysis outside archive masters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import importlib
from importlib.machinery import ModuleSpec
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import secrets
import shutil
import stat
import sys
import tempfile
import tomllib
from types import ModuleType
from typing import Any, Mapping, Sequence

from text_preserver.capture.plan import CAPTURE_ID_RE
from text_preserver.config import SAFE_ID_RE, CollectionConfig, Config
from text_preserver.manifest import MANIFEST_NAME, verify_capture
from text_preserver.recipe_bundle import (
    RecipeBundleError,
    scan_declared_assets,
    scan_recipe_directory,
    verify_bundle_manifest,
)


class AnalysisError(RuntimeError):
    """Raised when preservation analysis cannot run safely."""


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


@dataclass(frozen=True)
class ReaderResult:
    capture_directory: Path
    output_directory: Path
    index_path: Path
    metadata_path: Path
    metadata: Mapping[str, Any]
    current_index_path: Path | None = None
    current_reader_updated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "capture_directory": str(self.capture_directory),
            "output_directory": str(self.output_directory),
            "index_path": str(self.index_path),
            "metadata_path": str(self.metadata_path),
            "current_index_path": (
                str(self.current_index_path) if self.current_index_path is not None else None
            ),
            "current_reader_updated": self.current_reader_updated,
            "metadata": dict(self.metadata),
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
    pointer_names: list[str] = []
    try:
        for entry in collection_root.iterdir():
            name = entry.name
            if name == "LATEST":
                pointer_names.append(name)
                continue
            if not name.startswith("LATEST-"):
                continue
            source_id = name.removeprefix("LATEST-")
            if SAFE_ID_RE.fullmatch(source_id) is None:
                raise AnalysisError(f"unsafe source capture pointer name: {entry}")
            pointer_names.append(name)
    except OSError as exc:
        raise AnalysisError(f"cannot inspect capture pointers under {collection_root}: {exc}") from exc
    if not pointer_names:
        raise AnalysisError(f"collection {collection.id} has no capture pointers")
    return tuple(
        (
            _read_analysis_capture_pointer(collection_root, name),
            name.removeprefix("LATEST-") if name != "LATEST" else None,
        )
        for name in sorted(pointer_names, key=lambda value: (value != "LATEST", value))
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


def build_static_reader(
    config: Config,
    collection_id: str,
    capture_path: str | Path | None = None,
) -> ReaderResult:
    """Build a capture-scoped static reader without changing archive masters."""
    collection, capture_directory, capture_id = _verified_collection_capture(
        config,
        collection_id,
        capture_path,
    )
    adapter_value = collection.analysis.get("reader_adapter")
    if adapter_value is None:
        adapter_value = collection.analysis.get("inventory_adapter")
    if not isinstance(adapter_value, str):
        raise AnalysisError(f"collection {collection.id} has no reader adapter")
    adapter_path, adapter_source, recipe_bundle_identity = _adapter_path(
        config,
        collection,
        capture_directory,
        adapter_value,
        prefer_preserved=False,
    )
    adapter, adapter_bytes = _load_adapter(adapter_path)
    write = getattr(adapter, "write_static_reader", None)
    render = getattr(adapter, "render_static_reader", None)
    relative_parent = Path("collections") / collection.id / "captures" / capture_id
    parent = _ensure_derived_directory(config.project.derived_root, relative_parent)
    output_directory = parent / "reader"
    files: dict[str, bytes] | None = None
    staging: Path | None = None
    try:
        if callable(write):
            staging = _create_derived_tree_staging(output_directory)
            try:
                payload = write(
                    capture_directory,
                    output_directory=staging,
                    expected_work_count=collection.analysis.get("expected_work_count", 0),
                )
            except Exception as exc:
                raise AnalysisError(f"reader adapter failed {adapter_path}: {exc}") from exc
            summary, warnings, status = _validate_streamed_reader_payload(payload, adapter_path)
            output_file_count, output_bytes = _validate_streamed_reader_tree(staging)
            summary["output_file_count"] = output_file_count
            summary["output_bytes"] = output_bytes
        elif callable(render):
            try:
                payload = render(
                    capture_directory,
                    expected_work_count=collection.analysis.get("expected_work_count", 0),
                )
            except Exception as exc:
                raise AnalysisError(f"reader adapter failed {adapter_path}: {exc}") from exc
            files, summary, warnings, status = _validate_reader_payload(payload, adapter_path)
        else:
            raise AnalysisError(
                "reader adapter does not export write_static_reader() or "
                f"render_static_reader(): {adapter_path}"
            )

        verification_after = verify_capture(capture_directory)
        if not verification_after.ok:
            raise AnalysisError(
                "capture changed during reader generation: "
                + "; ".join(verification_after.errors)
            )

        metadata: dict[str, Any] = {
            "schema_version": 2,
            "status": status,
            "collection_id": collection.id,
            "capture_id": capture_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "capture_manifest_sha256": _sha256(capture_directory / MANIFEST_NAME),
            "configuration_sha256": hashlib.sha256(config.input_bytes).hexdigest(),
            "recipe_bundle": recipe_bundle_identity,
            "expected_work_count": collection.analysis.get("expected_work_count", 0),
            "renderer": {
                "path": str(adapter_path),
                "source": adapter_source,
                "sha256": hashlib.sha256(adapter_bytes).hexdigest(),
            },
            "summary": summary,
            "warnings": warnings,
        }
        if staging is not None:
            generation_directory = _publish_derived_tree(
                output_directory,
                staging,
                metadata,
            )
            staging = None
        elif files is not None:
            generation_directory = _write_derived_tree(output_directory, files, metadata)
        else:
            raise AnalysisError("reader adapter produced no output")
    except (OSError, TypeError, ValueError) as exc:
        raise AnalysisError(f"cannot write static reader {output_directory}: {exc}") from exc
    finally:
        if staging is not None and staging.exists():
            _make_tree_writable(staging)
            shutil.rmtree(staging)
    current_reader_updated = status in {"complete", "complete_with_warnings"}
    if current_reader_updated:
        current_index_path = _update_current_reader_pointer(
            config.project.derived_root,
            collection.id,
            generation_directory,
        )
    else:
        current_index_path = _existing_current_reader_index(
            config.project.derived_root,
            collection.id,
        )
    return ReaderResult(
        capture_directory,
        output_directory,
        output_directory / "index.html",
        output_directory / "metadata.json",
        metadata,
        current_index_path,
        current_reader_updated,
    )


def _verified_collection_capture(
    config: Config,
    collection_id: str,
    capture_path: str | Path | None,
) -> tuple[CollectionConfig, Path, str]:
    collection = _find_collection(config, collection_id)
    if capture_path is not None:
        requested_path = Path(capture_path)
    else:
        collection_root = config.project.archive_root / "collections" / collection.id
        reader_source = collection.analysis.get("reader_source")
        source_pointer = collection_root / f"LATEST-{reader_source}"
        if isinstance(reader_source, str) and (
            source_pointer.is_symlink() or source_pointer.exists()
        ):
            requested_path = _read_capture_pointer(collection_root, source_pointer.name)
        else:
            requested_path = collection_root
    verification = verify_capture(requested_path)
    if not verification.ok:
        raise AnalysisError("capture fixity verification failed: " + "; ".join(verification.errors))
    capture_directory = verification.capture_directory
    capture_metadata = _read_json(capture_directory / "capture.json", "capture metadata")
    if capture_metadata.get("collection_id") != collection.id:
        raise AnalysisError(
            f"capture belongs to collection {capture_metadata.get('collection_id')!r}, not {collection.id!r}"
        )
    capture_id = capture_metadata.get("capture_id")
    if not isinstance(capture_id, str) or CAPTURE_ID_RE.fullmatch(capture_id) is None:
        raise AnalysisError(f"capture metadata has an unsafe capture ID: {capture_id!r}")
    if capture_id != capture_directory.name:
        raise AnalysisError(
            f"capture metadata ID {capture_id!r} does not match directory {capture_directory.name!r}"
        )
    return collection, capture_directory, capture_id


def current_reader_index(config: Config, collection_id: str) -> Path:
    """Return the safe stable index for a collection's current derived reader."""
    collection = _find_collection(config, collection_id)
    path = _existing_current_reader_index(config.project.derived_root, collection.id)
    if path is None:
        raise AnalysisError(f"collection {collection.id} has no current derived reader")
    return path


def _read_capture_pointer(collection_root: Path, name: str) -> Path:
    pointer = collection_root / name
    if pointer.is_symlink() or not pointer.is_file():
        raise AnalysisError(f"capture pointer is unavailable: {pointer}")
    try:
        value = pointer.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise AnalysisError(f"cannot read capture pointer {pointer}: {exc}") from exc
    relative = Path(value)
    if not value or relative.is_absolute() or ".." in relative.parts:
        raise AnalysisError(f"unsafe capture pointer: {pointer}")
    target = (collection_root / relative).resolve()
    if not target.is_relative_to(collection_root.resolve()):
        raise AnalysisError(f"capture pointer escapes collection directory: {pointer}")
    return target


def _update_current_reader_pointer(
    derived_root: Path,
    collection_id: str,
    reader_directory: Path,
) -> Path:
    collection_root = derived_root / "collections" / collection_id
    captures_root = collection_root / "captures"
    resolved_reader = reader_directory.resolve()
    if not resolved_reader.is_relative_to(captures_root.resolve()):
        raise AnalysisError(f"reader output escapes collection captures: {reader_directory}")
    current = collection_root / "reader"
    if current.is_symlink():
        if not current.resolve().is_relative_to(captures_root.resolve()):
            raise AnalysisError(f"current reader pointer escapes collection captures: {current}")
    elif current.exists():
        raise AnalysisError(f"current reader pointer is not a symlink: {current}")
    temporary = collection_root / f".reader-current-{secrets.token_hex(8)}"
    try:
        relative = os.path.relpath(resolved_reader, collection_root)
        os.symlink(relative, temporary, target_is_directory=True)
        os.replace(temporary, current)
        directory_descriptor = os.open(collection_root, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)
    return current / "index.html"


def _existing_current_reader_index(derived_root: Path, collection_id: str) -> Path | None:
    collection_root = derived_root / "collections" / collection_id
    captures_root = collection_root / "captures"
    current = collection_root / "reader"
    if not current.is_symlink():
        return None
    resolved = current.resolve()
    if not resolved.is_relative_to(captures_root.resolve()):
        raise AnalysisError(f"current reader pointer escapes collection captures: {current}")
    index = current / "index.html"
    if index.is_symlink() or not index.is_file():
        raise AnalysisError(f"current reader index is unavailable: {index}")
    return index


def _find_collection(config: Config, collection_id: str) -> CollectionConfig:
    for collection in config.collections:
        if collection.id == collection_id:
            return collection
    raise AnalysisError(f"unknown collection: {collection_id}")


def _adapter_path(
    config: Config,
    collection: CollectionConfig,
    capture_directory: Path,
    value: str,
    *,
    prefer_preserved: bool = True,
) -> tuple[Path, str, dict[str, Any]]:
    path = Path(value).expanduser()
    base = collection.recipe_path.parent if collection.recipe_path else config.path.parent
    bundle_root = capture_directory / "metadata" / "recipe-bundle"
    bundle_manifest = capture_directory / "metadata" / "recipe-bundle-manifest.json"
    captured_identity: dict[str, Any] | None = None
    captured_manifest: dict[str, Any] | None = None
    if (
        bundle_root.exists()
        or bundle_root.is_symlink()
        or bundle_manifest.exists()
        or bundle_manifest.is_symlink()
    ):
        try:
            captured_bundle, captured_manifest = verify_bundle_manifest(
                bundle_root,
                bundle_manifest,
                expected_collection_id=collection.id,
            )
        except RecipeBundleError as exc:
            raise AnalysisError(f"invalid captured recipe bundle: {exc}") from exc
        if captured_manifest["recipe_api"] != collection.recipe_api:
            raise AnalysisError(
                "captured recipe bundle API does not match the resolved collection"
            )
        captured_identity = {
            "source": "captured_bundle",
            "recipe_api": captured_manifest["recipe_api"],
            "sha256": captured_bundle.sha256,
        }
    if not path.is_absolute() and prefer_preserved:
        preserved_value = value
        if captured_manifest is not None and captured_manifest["recipe_api"] is not None:
            preserved_value = _captured_recipe_adapter(bundle_root, captured_manifest)
        preserved_path = Path(preserved_value)
        if preserved_path.is_absolute() or ".." in preserved_path.parts:
            raise AnalysisError(
                f"inventory adapter has an unsafe relative path: {preserved_value}"
            )
        if captured_identity is not None:
            preserved = bundle_root / preserved_path
            resolved_preserved = preserved.resolve()
            if (
                not resolved_preserved.is_relative_to(bundle_root.resolve())
                or preserved.is_symlink()
                or not preserved.is_file()
            ):
                raise AnalysisError(
                    f"captured recipe bundle has no regular inventory adapter: {preserved_value}"
                )
            return resolved_preserved, "preserved_capture", captured_identity

        legacy_root = capture_directory / "metadata" / "recipe-assets"
        legacy = legacy_root / preserved_path
        resolved_legacy = legacy.resolve()
        if (
            resolved_legacy.is_relative_to(legacy_root.resolve())
            and legacy.is_file()
            and not legacy.is_symlink()
        ):
            return resolved_legacy, "preserved_capture", {
                "source": "legacy_recipe_assets",
                "recipe_api": None,
                "sha256": None,
            }
    if not path.is_absolute():
        path = base / path
    resolved = path.resolve()
    if not resolved.is_relative_to(base.resolve()):
        raise AnalysisError(f"inventory adapter escapes its configuration directory: {value}")
    if resolved.is_symlink() or not resolved.is_file():
        raise AnalysisError(f"inventory adapter is not a regular file: {resolved}")
    return resolved, "current_recipe", _current_recipe_identity(config, collection)


def _captured_recipe_adapter(
    bundle_root: Path,
    manifest: Mapping[str, Any],
) -> str:
    recipe_path = bundle_root / "collection.toml"
    try:
        if recipe_path.is_symlink() or not recipe_path.is_file():
            raise AnalysisError("captured recipe bundle has no authoritative collection.toml")
        recipe = tomllib.loads(recipe_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise AnalysisError(f"cannot read captured recipe bundle collection.toml: {exc}") from exc
    if not isinstance(recipe, dict) or recipe.get("recipe_api") != manifest["recipe_api"]:
        raise AnalysisError("captured collection.toml has a mismatched recipe API")
    raw_collection = recipe.get("collection")
    if not isinstance(raw_collection, dict) or raw_collection.get("id") != manifest["collection_id"]:
        raise AnalysisError("captured collection.toml has a mismatched collection ID")
    raw_analysis = raw_collection.get("analysis")
    adapter = raw_analysis.get("inventory_adapter") if isinstance(raw_analysis, dict) else None
    if not isinstance(adapter, str) or not adapter:
        raise AnalysisError("captured collection.toml has no inventory adapter")
    return adapter


def _current_recipe_identity(
    config: Config,
    collection: CollectionConfig,
) -> dict[str, Any]:
    base = collection.recipe_path.parent if collection.recipe_path else config.path.parent
    try:
        if collection.recipe_path is not None:
            bundle = scan_recipe_directory(base)
            source = "current_recipe_bundle"
        else:
            declared = (
                value
                for key in (
                    "inventory_adapter",
                    "reader_adapter",
                    "normalizer",
                    "ciao_rules",
                )
                if isinstance((value := collection.analysis.get(key)), str)
            )
            bundle = scan_declared_assets(base, declared)
            source = "current_inline_assets"
    except RecipeBundleError as exc:
        raise AnalysisError(f"cannot inventory current recipe bundle: {exc}") from exc
    return {
        "source": source,
        "recipe_api": collection.recipe_api,
        "sha256": bundle.sha256,
    }


def _validate_reader_payload(
    payload: Any,
    adapter_path: Path,
) -> tuple[dict[str, bytes], dict[str, Any], list[str], str]:
    if not isinstance(payload, dict):
        raise AnalysisError(f"reader adapter returned an invalid payload: {adapter_path}")
    raw_files = payload.get("files")
    summary = payload.get("summary", {})
    warnings = payload.get("warnings", [])
    status = payload.get("status")
    if not isinstance(raw_files, Mapping) or not isinstance(summary, dict):
        raise AnalysisError(f"reader adapter returned invalid files or summary: {adapter_path}")
    if not isinstance(warnings, list) or not all(isinstance(item, str) for item in warnings):
        raise AnalysisError(f"reader adapter returned invalid warnings: {adapter_path}")
    if status not in {"complete", "complete_with_warnings", "incomplete"}:
        raise AnalysisError(f"reader adapter returned an invalid status: {adapter_path}")
    if len(raw_files) > 2_000:
        raise AnalysisError("reader adapter returned more than 2,000 files")
    files: dict[str, bytes] = {}
    total_size = 0
    for name, value in raw_files.items():
        if not isinstance(name, str) or "\\" in name:
            raise AnalysisError(f"reader adapter returned an unsafe path: {name!r}")
        path = PurePosixPath(name)
        if not path.parts or path.is_absolute() or ".." in path.parts:
            raise AnalysisError(f"reader adapter returned an unsafe path: {name!r}")
        if name == "metadata.json" or name.endswith("/"):
            raise AnalysisError(f"reader adapter returned a reserved path: {name!r}")
        if not isinstance(value, str):
            raise AnalysisError(f"reader adapter returned non-text content for {name!r}")
        encoded = value.encode("utf-8")
        total_size += len(encoded)
        if total_size > 128 * 1024 * 1024:
            raise AnalysisError("reader adapter output exceeds 128 MiB")
        files[name] = encoded
    if "index.html" not in files:
        raise AnalysisError("reader adapter did not return index.html")
    return files, summary, warnings, status


def _validate_streamed_reader_payload(
    payload: Any,
    adapter_path: Path,
) -> tuple[dict[str, Any], list[str], str]:
    if not isinstance(payload, dict):
        raise AnalysisError(f"reader adapter returned an invalid payload: {adapter_path}")
    if "files" in payload:
        raise AnalysisError(f"streaming reader adapter returned in-memory files: {adapter_path}")
    summary = payload.get("summary", {})
    warnings = payload.get("warnings", [])
    status = payload.get("status")
    if not isinstance(summary, dict):
        raise AnalysisError(f"reader adapter returned an invalid summary: {adapter_path}")
    if not isinstance(warnings, list) or not all(isinstance(item, str) for item in warnings):
        raise AnalysisError(f"reader adapter returned invalid warnings: {adapter_path}")
    if status not in {"complete", "complete_with_warnings", "incomplete"}:
        raise AnalysisError(f"reader adapter returned an invalid status: {adapter_path}")
    return summary, warnings, status


def _validate_streamed_reader_tree(root: Path) -> tuple[int, int]:
    if root.is_symlink() or not root.is_dir():
        raise AnalysisError("streaming reader output is not a regular directory")
    count = 0
    total_size = 0
    for path in root.rglob("*"):
        path_stat = path.lstat()
        if stat.S_ISLNK(path_stat.st_mode):
            raise AnalysisError(f"streaming reader output contains a symlink: {path}")
        if stat.S_ISDIR(path_stat.st_mode):
            continue
        if not stat.S_ISREG(path_stat.st_mode):
            raise AnalysisError(f"streaming reader output contains a special file: {path}")
        if path_stat.st_nlink != 1:
            raise AnalysisError(f"streaming reader output contains a hard link: {path}")
        relative = path.relative_to(root)
        relative_name = relative.as_posix()
        if "\\" in relative_name:
            raise AnalysisError(f"streaming reader output contains an unsafe path: {relative}")
        if relative_name == "metadata.json":
            raise AnalysisError("streaming reader output contains reserved metadata.json")
        count += 1
        if count > 2_000:
            raise AnalysisError("streaming reader adapter wrote more than 2,000 files")
        size = path.stat().st_size
        if size > 128 * 1024 * 1024:
            raise AnalysisError(f"streaming reader output file exceeds 128 MiB: {relative}")
        total_size += size
        if total_size > 512 * 1024 * 1024:
            raise AnalysisError("streaming reader adapter output exceeds 512 MiB")
        try:
            with path.open("r", encoding="utf-8") as stream:
                opened_stat = os.fstat(stream.fileno())
                if (opened_stat.st_dev, opened_stat.st_ino) != (
                    path_stat.st_dev,
                    path_stat.st_ino,
                ):
                    raise AnalysisError(
                        f"streaming reader output changed during validation: {relative}"
                    )
                while stream.read(1024 * 1024):
                    pass
        except UnicodeError as exc:
            raise AnalysisError(
                f"streaming reader output is not UTF-8 text: {relative}"
            ) from exc
        final_stat = path.lstat()
        if (
            final_stat.st_dev,
            final_stat.st_ino,
            final_stat.st_size,
            final_stat.st_mtime_ns,
        ) != (
            path_stat.st_dev,
            path_stat.st_ino,
            path_stat.st_size,
            path_stat.st_mtime_ns,
        ):
            raise AnalysisError(
                f"streaming reader output changed during validation: {relative}"
            )
    index = root / "index.html"
    if index.is_symlink() or not index.is_file():
        raise AnalysisError("streaming reader adapter did not write index.html")
    return count, total_size


def _load_adapter(path: Path) -> tuple[ModuleType, bytes]:
    directory_identity = hashlib.sha256(
        str(path.parent.resolve()).encode("utf-8")
    ).hexdigest()[:16]
    package_name = f"text_preserver_recipe_{directory_identity}"
    module_identity = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:16]
    name = f"{package_name}.adapter_{module_identity}"
    for loaded_name in tuple(sys.modules):
        if loaded_name == package_name or loaded_name.startswith(f"{package_name}."):
            sys.modules.pop(loaded_name, None)
    importlib.invalidate_caches()
    package = ModuleType(package_name)
    package.__file__ = str(path.parent)
    package.__package__ = package_name
    package.__path__ = [str(path.parent)]  # type: ignore[attr-defined]
    package.__spec__ = ModuleSpec(package_name, loader=None, is_package=True)
    package.__spec__.submodule_search_locations = [str(path.parent)]
    sys.modules[package_name] = package
    module = ModuleType(name)
    module.__file__ = str(path)
    module.__package__ = package_name
    sys.modules[name] = module
    previous_bytecode_setting = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        source = path.read_bytes()
        code = compile(source, str(path), "exec")
        exec(code, module.__dict__)
    except Exception as exc:
        for loaded_name in tuple(sys.modules):
            if loaded_name == package_name or loaded_name.startswith(f"{package_name}."):
                sys.modules.pop(loaded_name, None)
        raise AnalysisError(f"cannot load adapter {path}: {exc}") from exc
    finally:
        sys.dont_write_bytecode = previous_bytecode_setting
    return module, source


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


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
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
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


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


def _write_derived_tree(
    target: Path,
    files: Mapping[str, bytes],
    metadata: Mapping[str, Any],
) -> Path:
    staging = _create_derived_tree_staging(target)
    try:
        for name, content in files.items():
            destination = staging.joinpath(*PurePosixPath(name).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        return _publish_derived_tree(target, staging, metadata)
    finally:
        if staging.exists():
            _make_tree_writable(staging)
            shutil.rmtree(staging)


def _create_derived_tree_staging(target: Path) -> Path:
    generations = target.parent / "reader-generations"
    if generations.is_symlink() or (generations.exists() and not generations.is_dir()):
        raise AnalysisError(f"reader generations path is not a regular directory: {generations}")
    generations.mkdir(exist_ok=True)
    if target.is_symlink():
        if not target.resolve().is_relative_to(generations.resolve()):
            raise AnalysisError(f"derived reader pointer escapes its generations: {target}")
    elif target.exists() and not target.is_dir():
        raise AnalysisError(f"derived reader target is not a regular directory: {target}")
    return Path(tempfile.mkdtemp(dir=generations, prefix=".build-"))


def _publish_derived_tree(
    target: Path,
    staging: Path,
    metadata: Mapping[str, Any],
) -> Path:
    generations = target.parent / "reader-generations"
    resolved_staging = staging.resolve()
    if (
        staging.is_symlink()
        or not staging.is_dir()
        or not resolved_staging.is_relative_to(generations.resolve())
    ):
        raise AnalysisError(f"reader staging path escapes its generations: {staging}")
    link = target.parent / f".reader-link-{secrets.token_hex(8)}"
    legacy: Path | None = None
    try:
        (staging / "metadata.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        generation_id = hashlib.sha256(
            (metadata["created_at"] + metadata["renderer"]["sha256"]).encode("utf-8")
        ).hexdigest()[:20]
        generation = generations / generation_id
        _fsync_tree(staging)
        os.replace(staging, generation)
        _make_tree_read_only(generation)
        _fsync_tree(generation)
        _fsync_directory(generations)

        if target.exists() and not target.is_symlink():
            legacy = generations / f"legacy-{secrets.token_hex(8)}"
            os.replace(target, legacy)
        try:
            relative_generation = os.path.relpath(generation, target.parent)
            os.symlink(relative_generation, link, target_is_directory=True)
            os.replace(link, target)
            _fsync_directory(target.parent)
        except Exception:
            if legacy is not None and not target.exists():
                os.replace(legacy, target)
                legacy = None
            raise
        return generation
    finally:
        link.unlink(missing_ok=True)


def _make_tree_read_only(root: Path) -> None:
    for path in root.rglob("*"):
        path.chmod(0o555 if path.is_dir() else 0o444)
    root.chmod(0o555)


def _make_tree_writable(root: Path) -> None:
    root.chmod(0o755)
    for path in root.rglob("*"):
        if path.is_dir():
            path.chmod(0o755)


def _fsync_tree(root: Path) -> None:
    directories = [root]
    for path in root.rglob("*"):
        if path.is_dir():
            directories.append(path)
            continue
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    for directory in reversed(directories):
        _fsync_directory(directory)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_output_parents(root: Path, parent: Path) -> None:
    current = root
    for part in parent.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise AnalysisError(f"derived reader path must not contain symlinks: {current}")
        current.mkdir(exist_ok=True)
        if not current.is_dir():
            raise AnalysisError(f"derived reader component is not a directory: {current}")


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
