"""Run collection-specific preservation analysis outside archive masters."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType
from typing import Any, Mapping

from text_preserver.capture.plan import CAPTURE_ID_RE
from text_preserver.config import CollectionConfig, Config
from text_preserver.manifest import verify_capture


class AnalysisError(RuntimeError):
    """Raised when preservation analysis cannot run safely."""


@dataclass(frozen=True)
class AnalysisResult:
    capture_directory: Path
    report_path: Path
    report: Mapping[str, Any]

    @property
    def status(self) -> str:
        return str(self.report["status"])

    def to_dict(self) -> dict[str, Any]:
        return {
            "capture_directory": str(self.capture_directory),
            "report_path": str(self.report_path),
            "report": dict(self.report),
        }


def analyze_preservation(
    config: Config,
    collection_id: str,
    capture_path: str | Path | None = None,
) -> AnalysisResult:
    """Verify and analyze one capture, writing the report under derived data."""
    collection = _find_collection(config, collection_id)
    requested_path = (
        Path(capture_path)
        if capture_path is not None
        else config.project.archive_root / "collections" / collection.id
    )
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

    adapter_value = collection.analysis.get("inventory_adapter")
    if not isinstance(adapter_value, str):
        raise AnalysisError(f"collection {collection.id} has no inventory adapter")
    adapter_path, adapter_source = _adapter_path(
        config,
        collection,
        capture_directory,
        adapter_value,
    )
    adapter, adapter_bytes = _load_adapter(adapter_path)
    analyze = getattr(adapter, "analyze_capture", None)
    if not callable(analyze):
        raise AnalysisError(f"inventory adapter does not export analyze_capture(): {adapter_path}")

    try:
        report = analyze(
            capture_directory,
            expected_work_count=collection.analysis.get("expected_work_count", 0),
            required_representation_kinds=tuple(
                collection.analysis.get("required_representation_kinds", ())
            ),
            required_source_ids=tuple(
                source.id for source in collection.sources if source.required
            ),
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
    report.setdefault("schema_version", 1)
    report["analyzer"] = {
        "path": str(adapter_path),
        "source": adapter_source,
        "sha256": hashlib.sha256(adapter_bytes).hexdigest(),
    }
    if adapter_source != "preserved_capture":
        warnings = report.setdefault("warnings", [])
        if not isinstance(warnings, list):
            raise AnalysisError("inventory adapter returned invalid warnings")
        warnings.append("analysis used the current recipe adapter; capture has no adapter snapshot")
        if report["status"] == "complete":
            report["status"] = "complete_with_warnings"

    verification_after = verify_capture(capture_directory)
    if not verification_after.ok:
        raise AnalysisError(
            "capture changed during analysis: " + "; ".join(verification_after.errors)
        )

    relative_report = Path("collections") / collection.id / "captures" / capture_id
    report_path = config.project.derived_root / relative_report / "completeness.json"
    try:
        report_directory = _ensure_derived_directory(
            config.project.derived_root,
            relative_report,
        )
        report_path = report_directory / "completeness.json"
        _write_json(report_path, report)
    except (OSError, TypeError, ValueError) as exc:
        raise AnalysisError(f"cannot write completeness report {report_path}: {exc}") from exc
    return AnalysisResult(capture_directory, report_path, report)


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
) -> tuple[Path, str]:
    path = Path(value).expanduser()
    base = collection.recipe_path.parent if collection.recipe_path else config.path.parent
    if not path.is_absolute():
        if ".." in path.parts:
            raise AnalysisError(f"inventory adapter has an unsafe relative path: {value}")
        preserved_root = capture_directory / "metadata" / "recipe-assets"
        preserved = preserved_root / path
        resolved_preserved = preserved.resolve()
        if (
            resolved_preserved.is_relative_to(preserved_root.resolve())
            and preserved.is_file()
            and not preserved.is_symlink()
        ):
            return resolved_preserved, "preserved_capture"
    if not path.is_absolute():
        path = base / path
    resolved = path.resolve()
    if not resolved.is_relative_to(base.resolve()):
        raise AnalysisError(f"inventory adapter escapes its configuration directory: {value}")
    if resolved.is_symlink() or not resolved.is_file():
        raise AnalysisError(f"inventory adapter is not a regular file: {resolved}")
    return resolved, "current_recipe"


def _load_adapter(path: Path) -> tuple[ModuleType, bytes]:
    identity = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
    name = f"text_preserver_recipe_{identity}"
    module = ModuleType(name)
    module.__file__ = str(path)
    module.__package__ = ""
    sys.modules[name] = module
    try:
        source = path.read_bytes()
        code = compile(source, str(path), "exec")
        exec(code, module.__dict__)
    except Exception as exc:
        sys.modules.pop(name, None)
        raise AnalysisError(f"cannot load inventory adapter {path}: {exc}") from exc
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
