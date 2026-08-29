"""Run collection-specific preservation analysis outside archive masters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import secrets
import shutil
import stat
import sys
import tempfile
from types import ModuleType
from typing import Any, Mapping

from text_preserver.capture.plan import CAPTURE_ID_RE
from text_preserver.config import CollectionConfig, Config
from text_preserver.manifest import MANIFEST_NAME, verify_capture


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
        prefer_preserved=collection.analysis.get("prefer_preserved_adapter", True),
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
        warnings.append(
            "analysis used the current recipe adapter; result may differ from the capture-time assessment"
        )
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
    adapter_value = collection.analysis.get("inventory_adapter")
    if not isinstance(adapter_value, str):
        raise AnalysisError(f"collection {collection.id} has no reader adapter")
    adapter_path, adapter_source = _adapter_path(
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
                "inventory adapter does not export write_static_reader() or "
                f"render_static_reader(): {adapter_path}"
            )

        verification_after = verify_capture(capture_directory)
        if not verification_after.ok:
            raise AnalysisError(
                "capture changed during reader generation: "
                + "; ".join(verification_after.errors)
            )

        metadata: dict[str, Any] = {
            "schema_version": 1,
            "status": status,
            "collection_id": collection.id,
            "capture_id": capture_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "capture_manifest_sha256": _sha256(capture_directory / MANIFEST_NAME),
            "configuration_sha256": hashlib.sha256(config.input_bytes).hexdigest(),
            "recipe_sha256": (
                hashlib.sha256(config.recipe_input_bytes[collection.recipe_path]).hexdigest()
                if collection.recipe_path is not None
                else None
            ),
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
) -> tuple[Path, str]:
    path = Path(value).expanduser()
    base = collection.recipe_path.parent if collection.recipe_path else config.path.parent
    if not path.is_absolute() and prefer_preserved:
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
