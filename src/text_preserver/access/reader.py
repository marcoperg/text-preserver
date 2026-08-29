"""Build static readers outside archive masters."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import shutil
import stat
import tempfile
from typing import Any, Iterator, Mapping

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
from text_preserver.preservation.fixity import (
    MANIFEST_NAME,
    is_complete_full_capture,
    verify_capture,
)


READER_SCHEMA_VERSION = 3


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
    initial_manifest_sha256 = _sha256(capture_directory / MANIFEST_NAME)
    capture_metadata = _read_json(capture_directory / "capture.json", "capture metadata")
    selected_source_ids = capture_metadata.get("selected_sources")
    if selected_source_ids is None:
        sources = capture_metadata.get("sources")
        if isinstance(sources, list):
            selected_source_ids = [
                source.get("source_id") if isinstance(source, dict) else None
                for source in sources
            ]
    if (
        not isinstance(selected_source_ids, list)
        or not all(
            isinstance(value, str) and SAFE_ID_RE.fullmatch(value) is not None
            for value in selected_source_ids
        )
        or len(selected_source_ids) != len(set(selected_source_ids))
    ):
        raise AnalysisError("capture metadata has unsafe selected source IDs")
    expected_work_count = collection.analysis.get("expected_work_count", 0)
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
    if callable(write):
        entry_point = "write_static_reader"
    elif callable(render):
        entry_point = "render_static_reader"
    else:
        raise AnalysisError(
            "reader adapter does not export write_static_reader() or "
            f"render_static_reader(): {adapter_path}"
        )
    renderer_sha256 = hashlib.sha256(adapter_bytes).hexdigest()
    build_inputs: dict[str, Any] = {
        "schema_version": READER_SCHEMA_VERSION,
        "collection_id": collection.id,
        "capture_manifest_sha256": initial_manifest_sha256,
        "selected_source_ids": sorted(selected_source_ids),
        "recipe_bundle": {
            "recipe_api": recipe_bundle_identity.get("recipe_api"),
            "sha256": recipe_bundle_identity.get("sha256"),
        },
        "renderer": {
            "sha256": renderer_sha256,
            "entry_point": entry_point,
        },
        "expected_work_count": expected_work_count,
    }
    build_key = hashlib.sha256(_canonical_json(build_inputs)).hexdigest()
    relative_parent = Path("collections") / collection.id / "captures" / capture_id
    parent = _ensure_derived_directory(config.project.derived_root, relative_parent)
    output_directory = parent / "reader"
    staging: Path | None = _create_derived_tree_staging(output_directory)
    try:
        if callable(write):
            try:
                payload = write(
                    capture_directory,
                    output_directory=staging,
                    expected_work_count=expected_work_count,
                )
            except Exception as exc:
                raise AnalysisError(f"reader adapter failed {adapter_path}: {exc}") from exc
            summary, warnings, status = _validate_streamed_reader_payload(payload, adapter_path)
        elif callable(render):
            try:
                payload = render(
                    capture_directory,
                    expected_work_count=expected_work_count,
                )
            except Exception as exc:
                raise AnalysisError(f"reader adapter failed {adapter_path}: {exc}") from exc
            files, summary, warnings, status = _validate_reader_payload(payload, adapter_path)
            _materialize_reader_files(staging, files)

        output_tree = _validate_streamed_reader_tree(staging)
        summary["output_file_count"] = output_tree["file_count"]
        summary["output_bytes"] = output_tree["total_bytes"]
        verification_after = verify_capture(capture_directory)
        final_manifest_sha256 = _sha256(capture_directory / MANIFEST_NAME)
        if not verification_after.ok or final_manifest_sha256 != initial_manifest_sha256:
            errors = list(verification_after.errors)
            if final_manifest_sha256 != initial_manifest_sha256:
                errors.append("fixity manifest digest changed")
            raise AnalysisError(
                "capture changed during reader generation: "
                + "; ".join(errors)
            )

        metadata: dict[str, Any] = {
            "schema_version": READER_SCHEMA_VERSION,
            "status": status,
            "collection_id": collection.id,
            "capture_id": capture_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "capture_manifest_sha256": initial_manifest_sha256,
            "configuration_sha256": hashlib.sha256(config.input_bytes).hexdigest(),
            "recipe_bundle": recipe_bundle_identity,
            "expected_work_count": expected_work_count,
            "renderer": {
                "path": str(adapter_path),
                "source": adapter_source,
                "sha256": renderer_sha256,
                "entry_point": entry_point,
            },
            "build_key": build_key,
            "build_inputs": build_inputs,
            "output_tree": output_tree,
            "summary": summary,
            "warnings": warnings,
        }
        collection_root = parent.parent.parent
        with _reader_publication_lock(collection_root):
            generation_directory, metadata = _publish_derived_tree(
                output_directory,
                staging,
                metadata,
            )
            staging = None
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
    except (OSError, TypeError, ValueError) as exc:
        raise AnalysisError(f"cannot write static reader {output_directory}: {exc}") from exc
    finally:
        if staging is not None and staging.exists():
            _make_tree_writable(staging)
            shutil.rmtree(staging)
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
    require_full_capture = False
    if capture_path is not None:
        requested_path = Path(capture_path)
    else:
        collection_root = config.project.archive_root / "collections" / collection.id
        canonical = collection_root / "LATEST-ACQUIRED"
        legacy = collection_root / "LATEST"
        if canonical.is_symlink() or canonical.exists():
            requested_path = _read_capture_pointer(collection_root, canonical.name)
            require_full_capture = True
        elif legacy.is_symlink() or legacy.exists():
            requested_path = _read_capture_pointer(collection_root, legacy.name)
            require_full_capture = True
        else:
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
    if require_full_capture and not is_complete_full_capture(
        capture_metadata,
        (source.id for source in collection.sources),
    ):
        raise AnalysisError("full capture pointer target is incomplete or source-filtered")
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
    if (
        relative.is_absolute()
        or len(relative.parts) != 2
        or relative.parts[0] != "captures"
        or CAPTURE_ID_RE.fullmatch(relative.parts[1]) is None
    ):
        raise AnalysisError(f"unsafe capture pointer: {pointer}")
    unresolved_target = collection_root / relative
    if unresolved_target.is_symlink():
        raise AnalysisError(f"capture pointer targets a symlink: {pointer}")
    target = unresolved_target.resolve()
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
    canonical = collection_root / "LATEST-READER"
    _validated_current_reader_target(collection_root, require_pointer=False)
    relative_pointer = reader_directory.relative_to(collection_root).as_posix()
    previous_current = os.readlink(current) if current.is_symlink() else None
    if current.is_symlink():
        if not current.resolve().is_relative_to(captures_root.resolve()):
            raise AnalysisError(f"current reader pointer escapes collection captures: {current}")
    elif current.exists():
        raise AnalysisError(f"current reader pointer is not a symlink: {current}")
    temporary = collection_root / f".reader-current-{secrets.token_hex(8)}"
    current_replaced = False
    canonical_replaced = False
    try:
        relative = os.path.relpath(resolved_reader, collection_root)
        os.symlink(relative, temporary, target_is_directory=True)
        os.replace(temporary, current)
        current_replaced = True
        _atomic_write_text(canonical, relative_pointer + "\n")
        canonical_replaced = True
        directory_descriptor = os.open(collection_root, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except Exception:
        if current_replaced and not canonical_replaced:
            if previous_current is None:
                current.unlink(missing_ok=True)
            else:
                os.symlink(previous_current, temporary, target_is_directory=True)
                os.replace(temporary, current)
        raise
    finally:
        temporary.unlink(missing_ok=True)
    return current / "index.html"


def _existing_current_reader_index(derived_root: Path, collection_id: str) -> Path | None:
    collection_root = derived_root / "collections" / collection_id
    resolved = _validated_current_reader_target(collection_root, require_pointer=True)
    if resolved is None:
        return None
    current = collection_root / "reader"
    index = current / "index.html"
    if index.is_symlink() or not index.is_file():
        raise AnalysisError(f"current reader index is unavailable: {index}")
    return index


def _validated_current_reader_target(
    collection_root: Path,
    *,
    require_pointer: bool,
) -> Path | None:
    captures_root = collection_root / "captures"
    current = collection_root / "reader"
    canonical = collection_root / "LATEST-READER"
    canonical_present = canonical.is_symlink() or canonical.exists()
    if canonical_present:
        if canonical.is_symlink() or not canonical.is_file():
            raise AnalysisError(f"current reader pointer is not a regular file: {canonical}")
        try:
            value = canonical.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            raise AnalysisError(f"cannot read current reader pointer {canonical}: {exc}") from exc
        relative = Path(value)
        if (
            relative.is_absolute()
            or len(relative.parts) != 4
            or relative.parts[0] != "captures"
            or CAPTURE_ID_RE.fullmatch(relative.parts[1]) is None
            or relative.parts[2] != "reader-generations"
            or not _is_sha256(relative.parts[3])
        ):
            raise AnalysisError(f"unsafe current reader pointer content: {canonical}")
        target = collection_root / relative
        if target.is_symlink() or not target.is_dir():
            raise AnalysisError(f"current reader pointer target is unavailable: {canonical}")
        resolved = target.resolve()
        if not resolved.is_relative_to(captures_root.resolve()):
            raise AnalysisError(f"current reader pointer escapes collection captures: {canonical}")
        if not current.is_symlink() or current.resolve() != resolved:
            raise AnalysisError(f"current reader pointers disagree: {canonical} and {current}")
        metadata = _read_json(target / "metadata.json", "current reader metadata")
        if (
            metadata.get("schema_version") != READER_SCHEMA_VERSION
            or metadata.get("build_key") != relative.parts[3]
            or metadata.get("status") not in {"complete", "complete_with_warnings"}
            or metadata.get("collection_id") != collection_root.name
            or metadata.get("capture_id") != relative.parts[1]
        ):
            raise AnalysisError(f"current reader metadata does not match pointer: {canonical}")
        build_inputs = metadata.get("build_inputs")
        if not isinstance(build_inputs, dict) or hashlib.sha256(
            _canonical_json(build_inputs)
        ).hexdigest() != relative.parts[3]:
            raise AnalysisError(f"current reader build inputs do not match pointer: {canonical}")
        if metadata.get("output_tree") != _validate_streamed_reader_tree(
            target, allow_metadata=True
        ):
            raise AnalysisError(f"current reader output tree does not match metadata: {canonical}")
        return resolved
    if not current.is_symlink():
        if require_pointer and (current.exists() or current.is_symlink()):
            raise AnalysisError(f"current reader pointer is not a symlink: {current}")
        return None
    resolved = current.resolve()
    if not resolved.is_relative_to(captures_root.resolve()):
        raise AnalysisError(f"current reader pointer escapes collection captures: {current}")
    return resolved


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


def _validate_streamed_reader_tree(
    root: Path,
    *,
    allow_metadata: bool = False,
) -> dict[str, Any]:
    if root.is_symlink() or not root.is_dir():
        raise AnalysisError("streaming reader output is not a regular directory")
    count = 0
    directory_count = 0
    total_size = 0
    entries: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=lambda value: value.relative_to(root).as_posix()):
        path_stat = path.lstat()
        if stat.S_ISLNK(path_stat.st_mode):
            raise AnalysisError(f"streaming reader output contains a symlink: {path}")
        if stat.S_ISDIR(path_stat.st_mode):
            directory_count += 1
            entries.append({"path": path.relative_to(root).as_posix(), "type": "directory"})
            continue
        if not stat.S_ISREG(path_stat.st_mode):
            raise AnalysisError(f"streaming reader output contains a special file: {path}")
        if path_stat.st_nlink != 1:
            raise AnalysisError(f"streaming reader output contains a hard link: {path}")
        relative = path.relative_to(root)
        relative_name = relative.as_posix()
        if "\\" in relative_name:
            raise AnalysisError(f"streaming reader output contains an unsafe path: {relative}")
        if relative_name == "metadata.json" and allow_metadata:
            continue
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
        entries.append(
            {
                "path": relative_name,
                "type": "file",
                "size": size,
                "sha256": _sha256(path),
            }
        )
    index = root / "index.html"
    if index.is_symlink() or not index.is_file():
        raise AnalysisError("streaming reader adapter did not write index.html")
    return {
        "sha256": hashlib.sha256(_canonical_json(entries)).hexdigest(),
        "file_count": count,
        "directory_count": directory_count,
        "total_bytes": total_size,
    }


def _materialize_reader_files(root: Path, files: Mapping[str, bytes]) -> None:
    for name, content in files.items():
        destination = root.joinpath(*PurePosixPath(name).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)


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


@contextmanager
def _reader_publication_lock(collection_root: Path) -> Iterator[None]:
    lock_path = collection_root / ".reader.lock"
    if lock_path.is_symlink() or (lock_path.exists() and not lock_path.is_file()):
        raise AnalysisError(f"reader publication lock is unsafe: {lock_path}")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _publish_derived_tree(
    target: Path,
    staging: Path,
    metadata: Mapping[str, Any],
) -> tuple[Path, dict[str, Any]]:
    generations = target.parent / "reader-generations"
    resolved_staging = staging.resolve()
    if (
        staging.is_symlink()
        or not staging.is_dir()
        or not resolved_staging.is_relative_to(generations.resolve())
    ):
        raise AnalysisError(f"reader staging path escapes its generations: {staging}")
    candidate = dict(metadata)
    (staging / "metadata.json").write_text(
        json.dumps(candidate, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    generation = generations / str(candidate["build_key"])
    if generation.is_symlink():
        _quarantine_reader_candidate(
            target.parent,
            staging,
            candidate,
            "deterministic generation path is a symlink",
            None,
        )
    if generation.exists():
        existing: dict[str, Any] | None = None
        try:
            existing = _read_json(generation / "metadata.json", "reader metadata")
            existing_tree = _validate_streamed_reader_tree(generation, allow_metadata=True)
            _validate_existing_generation(generation, existing, existing_tree, candidate)
        except AnalysisError as exc:
            expected = None
            if existing is not None:
                value = existing.get("output_tree")
                if isinstance(value, dict) and isinstance(value.get("sha256"), str):
                    expected = value["sha256"]
            _quarantine_reader_candidate(
                target.parent,
                staging,
                candidate,
                str(exc),
                expected,
            )
        shutil.rmtree(staging)
        _update_capture_reader_link(target, generation)
        assert existing is not None
        return generation, existing
    renamed = False
    try:
        _fsync_tree(staging)
        os.rename(staging, generation)
        renamed = True
        _make_tree_read_only(generation)
        _fsync_tree(generation)
        _fsync_directory(generations)
    except OSError as exc:
        if not renamed and generation.exists() and not generation.is_symlink():
            try:
                existing = _read_json(generation / "metadata.json", "reader metadata")
                existing_tree = _validate_streamed_reader_tree(generation, allow_metadata=True)
                _validate_existing_generation(generation, existing, existing_tree, candidate)
            except AnalysisError as validation_exc:
                _quarantine_reader_candidate(
                    target.parent,
                    staging,
                    candidate,
                    str(validation_exc),
                    None,
                )
            shutil.rmtree(staging)
            _update_capture_reader_link(target, generation)
            return generation, existing
        if renamed:
            try:
                _make_tree_writable(generation)
                shutil.rmtree(generation)
                _fsync_directory(generations)
            except OSError:
                pass
        raise AnalysisError(f"cannot publish reader generation {generation}: {exc}") from exc
    _update_capture_reader_link(target, generation)
    return generation, candidate


def _validate_existing_generation(
    generation: Path,
    existing: Mapping[str, Any],
    existing_tree: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> None:
    if existing.get("schema_version") != READER_SCHEMA_VERSION:
        raise AnalysisError(f"existing reader generation has unsupported schema: {generation}")
    if existing.get("build_key") != candidate.get("build_key"):
        raise AnalysisError(f"existing reader generation has mismatched build key: {generation}")
    if existing.get("build_inputs") != candidate.get("build_inputs"):
        raise AnalysisError(f"existing reader generation has mismatched build inputs: {generation}")
    if existing.get("output_tree") != existing_tree:
        raise AnalysisError(f"existing reader generation tree is corrupt: {generation}")
    for field in ("output_tree", "status", "summary", "warnings"):
        if existing.get(field) != candidate.get(field):
            raise AnalysisError(
                f"reader reproducibility mismatch for {field}: {generation}"
            )


def _quarantine_reader_candidate(
    capture_root: Path,
    staging: Path,
    metadata: Mapping[str, Any],
    reason: str,
    expected_tree_sha256: str | None,
) -> None:
    build_key = str(metadata["build_key"])
    output_tree = metadata.get("output_tree")
    actual_tree_sha256 = (
        output_tree.get("sha256") if isinstance(output_tree, Mapping) else None
    )
    report = {
        "reason": reason,
        "build_key": build_key,
        "expected_tree_sha256": expected_tree_sha256,
        "actual_tree_sha256": actual_tree_sha256,
    }
    (staging / "reproducibility.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    quarantine_root = capture_root / "reader-quarantine"
    if quarantine_root.is_symlink() or (
        quarantine_root.exists() and not quarantine_root.is_dir()
    ):
        raise AnalysisError(f"reader quarantine path is unsafe: {quarantine_root}")
    quarantine_root.mkdir(exist_ok=True)
    quarantine = quarantine_root / f"{build_key}-{secrets.token_hex(6)}"
    os.rename(staging, quarantine)
    _make_tree_read_only(quarantine)
    _fsync_tree(quarantine)
    _fsync_directory(quarantine_root)
    raise AnalysisError(
        "reader reproducibility failure: "
        f"reason={reason}; key={build_key}; expected={expected_tree_sha256}; "
        f"actual={actual_tree_sha256}"
    )


def _update_capture_reader_link(target: Path, generation: Path) -> None:
    link = target.parent / f".reader-link-{secrets.token_hex(8)}"
    legacy: Path | None = None
    try:
        if target.exists() and not target.is_symlink():
            legacy = target.parent / "reader-generations" / f"legacy-{secrets.token_hex(8)}"
            os.rename(target, legacy)
        relative_generation = os.path.relpath(generation, target.parent)
        os.symlink(relative_generation, link, target_is_directory=True)
        os.replace(link, target)
        _fsync_directory(target.parent)
    except Exception:
        if legacy is not None and not target.exists():
            os.rename(legacy, target)
        raise
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


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _atomic_write_text(path: Path, value: str) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise AnalysisError(f"reader pointer is not a regular file: {path}")
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
