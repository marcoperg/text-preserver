"""Execute capture plans while preserving provenance and failures."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import fcntl
import gzip
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import zlib
from types import MappingProxyType
from typing import Any, Iterator, Mapping, Sequence

from text_preserver import __version__
from text_preserver.preservation.capture.engines.wget import WgetCommand
from text_preserver.preservation.capture.plan import CAPTURE_ID_RE, CapturePlan, plan_capture
from text_preserver.config import CollectionConfig, Config, SourceConfig
from text_preserver.preservation.fixity import (
    ManifestError,
    finalize_capture,
    is_complete_full_capture,
    verify_capture,
)
from text_preserver.preservation.recipe_bundle import (
    RecipeBundleError,
    copy_bundle,
    scan_declared_assets,
    scan_recipe_directory,
)


CAPTURE_MANIFEST_SCHEMA_VERSION = 2
MAX_CDX_FILES = 1_000
MAX_CDX_FILE_BYTES = 512 * 1024 * 1024
MAX_CDX_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_CDX_LINE_BYTES = 1024 * 1024
MAX_CDX_RECORDS = 10_000_000
MAX_WARC_INSPECTION_FILES = 256
MAX_WARC_INSPECTION_BYTES_PER_FILE = 1024 * 1024
MAX_WARC_INSPECTION_BYTES_TOTAL = 64 * 1024 * 1024


class CaptureExecutionError(RuntimeError):
    """Raised when a capture cannot be started safely."""


class _SourceInterrupted(KeyboardInterrupt):
    def __init__(self, result: dict[str, Any]) -> None:
        super().__init__()
        self.result = result


@dataclass(frozen=True)
class CaptureResult:
    capture_directory: Path
    metadata: Mapping[str, Any]
    # Compatibility name: this now reports updates to canonical LATEST-ACQUIRED.
    latest_updated: bool = False
    source_latest_updated: tuple[str, ...] = ()

    @property
    def status(self) -> str:
        return str(self.metadata["status"])

    def to_dict(self) -> dict[str, Any]:
        return {
            "capture_directory": str(self.capture_directory),
            "latest_updated": self.latest_updated,
            "source_latest_updated": list(self.source_latest_updated),
            **dict(self.metadata),
        }


def execute_capture(
    config: Config,
    collection_id: str,
    *,
    source_ids: Sequence[str] = (),
    capture_id: str | None = None,
    operator_note: str | None = None,
) -> CaptureResult:
    """Execute a new capture sequentially under a collection lock."""
    actual_capture_id = capture_id or _generate_capture_id()
    plan = plan_capture(
        config,
        collection_id,
        source_ids=source_ids,
        capture_id=actual_capture_id,
    )
    collection = _collection(config, collection_id)
    if not collection.enabled:
        raise CaptureExecutionError(
            f"collection {collection.id} is disabled; enable it before capture execution"
        )
    wget, wget_version = _validated_wget()
    plan = replace(
        plan,
        commands=tuple(
            replace(command, argv=(wget, *command.argv[1:])) for command in plan.commands
        ),
    )

    with collection_lock(config.project.archive_root, collection.id):
        try:
            plan.capture_directory.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise CaptureExecutionError(
                f"capture directory already exists: {plan.capture_directory}"
            ) from exc
        except OSError as exc:
            raise CaptureExecutionError(
                f"cannot create capture directory {plan.capture_directory}: {exc}"
            ) from exc
        with termination_signals_as_interrupts():
            return _execute_plan(
                config,
                collection,
                plan,
                operator_note,
                wget_version,
                eligible_for_latest=not source_ids,
            )


@contextmanager
def collection_lock(archive_root: Path, collection_id: str) -> Iterator[Path]:
    """Hold a nonblocking exclusive lock for one collection."""
    lock_root = archive_root / "locks"
    lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = lock_root / f"{collection_id}.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise CaptureExecutionError(f"cannot open collection lock {lock_path}: {exc}") from exc
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CaptureExecutionError(
                f"another capture is active for collection {collection_id}"
            ) from exc
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
            raise CaptureExecutionError(f"unsafe collection lock file: {lock_path}")
        yield lock_path
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


@contextmanager
def termination_signals_as_interrupts() -> Iterator[None]:
    """Convert normal termination signals into the interruption status path."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    signals = [signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        signals.append(signal.SIGHUP)
    previous = {number: signal.getsignal(number) for number in signals}

    def interrupt(_number: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    try:
        for number in signals:
            signal.signal(number, interrupt)
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def _execute_plan(
    config: Config,
    collection: CollectionConfig,
    plan: CapturePlan,
    operator_note: str | None,
    wget_version: str,
    eligible_for_latest: bool,
) -> CaptureResult:
    started_at = _utc_now()
    capture_metadata: dict[str, Any] = {
        "schema_version": CAPTURE_MANIFEST_SCHEMA_VERSION,
        "capture_id": plan.capture_id,
        "collection_id": plan.collection_id,
        "status": "running",
        "started_at": started_at,
        "ended_at": None,
        "selected_sources": [command.source_id for command in plan.commands],
        "sources": [],
    }
    if operator_note:
        capture_metadata["operator_note"] = operator_note

    capture_path = plan.capture_directory / "capture.json"
    _write_json(capture_path, capture_metadata)
    results: list[dict[str, Any]] = []
    try:
        metadata_root = plan.capture_directory / "metadata"
        metadata_root.mkdir()
        (metadata_root / "input-config.toml").write_bytes(config.input_bytes)
        if collection.recipe_path is not None:
            (metadata_root / "input-collection-recipe.toml").write_bytes(
                config.recipe_input_bytes[collection.recipe_path]
            )
        _preserve_recipe_bundle(config, collection, metadata_root)
        _write_json(
            metadata_root / "environment.json",
            _environment_metadata(wget_version),
        )
        _write_json(
            metadata_root / "resolved-collection.json",
            _collection_dict(collection),
        )
        source_by_id = {source.id: source for source in collection.sources}
        for command in plan.commands:
            source = source_by_id[command.source_id]
            try:
                result = _execute_source(command, source)
            except _SourceInterrupted:
                raise
            except KeyboardInterrupt:
                result = _interrupted_source_result(source, command.working_directory)
                raise _SourceInterrupted(result)
            except OSError as exc:
                result = _failed_source_result(source, str(exc), command.working_directory)
            results.append(result)
            capture_metadata["sources"] = results
            _write_json(capture_path, capture_metadata)
    except _SourceInterrupted as exc:
        results.append(exc.result)
        capture_metadata["status"] = "interrupted"
        capture_metadata["ended_at"] = _utc_now()
        capture_metadata["sources"] = results
        _write_json(capture_path, capture_metadata)
        raise KeyboardInterrupt from exc
    except KeyboardInterrupt:
        capture_metadata["status"] = "interrupted"
        capture_metadata["ended_at"] = _utc_now()
        capture_metadata["sources"] = results
        _write_json(capture_path, capture_metadata)
        raise
    except Exception as exc:
        capture_metadata["status"] = "failed"
        capture_metadata["ended_at"] = _utc_now()
        capture_metadata["sources"] = results
        capture_metadata["error"] = str(exc)
        _write_json(capture_path, capture_metadata)
        raise CaptureExecutionError(
            f"capture failed during setup or finalization: {exc}"
        ) from exc

    capture_metadata["sources"] = results
    capture_metadata["status"] = _aggregate_status(results)
    capture_metadata["ended_at"] = _utc_now()
    _write_json(capture_path, capture_metadata)
    try:
        finalize_capture(plan.capture_directory)
    except (ManifestError, OSError) as exc:
        capture_metadata["status"] = "failed"
        capture_metadata["error"] = f"fixity finalization failed: {exc}"
        _write_json(capture_path, capture_metadata)
        raise CaptureExecutionError(f"capture fixity finalization failed: {exc}") from exc
    verification = verify_capture(plan.capture_directory)
    if not verification.ok:
        raise CaptureExecutionError(
            "capture failed immediate fixity verification: " + "; ".join(verification.errors)
        )
    latest_updated = (
        eligible_for_latest
        and capture_metadata["status"] == "complete"
        and _update_latest(plan.capture_directory)
    )
    source_latest_updated = tuple(
        str(result["source_id"])
        for result in results
        if result.get("status") in {"complete", "complete_with_warnings"}
        and _update_latest(plan.capture_directory, source_id=str(result["source_id"]))
    )
    return CaptureResult(
        capture_directory=plan.capture_directory,
        metadata=MappingProxyType(capture_metadata),
        latest_updated=latest_updated,
        source_latest_updated=source_latest_updated,
    )


def _execute_source(command: WgetCommand, source: SourceConfig) -> dict[str, Any]:
    for directory in command.required_directories:
        directory.mkdir(parents=True, exist_ok=True)
    source_root = command.working_directory
    metadata_root = source_root / "metadata"
    _write_text(source_root / "seeds.txt", "\n".join(source.seeds) + "\n")
    _write_json(metadata_root / "command.json", command.to_dict())
    _write_json(metadata_root / "resolved-source.json", _source_dict(source))

    result_path = metadata_root / "result.json"
    result: dict[str, Any] = {
        "source_id": source.id,
        "required": source.required,
        "status": "running",
        "started_at": _utc_now(),
        "ended_at": None,
        "exit_code": None,
        "downloaded_files": 0,
        "downloaded_bytes": 0,
        "payloads": _empty_payloads(),
        "warnings": [],
        "error": None,
    }
    _write_json(result_path, result)
    try:
        process = _run_wget(command)
        result["exit_code"] = process.returncode
        if process.stderr.strip():
            result["warnings"].append(process.stderr.strip())
    except KeyboardInterrupt:
        result["status"] = "interrupted"
        _record_payloads(result, source_root)
        result["ended_at"] = _utc_now()
        _write_json(result_path, result)
        raise _SourceInterrupted(result)
    except OSError as exc:
        result["error"] = str(exc)
        result["status"] = (
            "partial" if _record_payloads(result, source_root) else "failed"
        )
    else:
        has_payload = _record_payloads(result, source_root)
        if process.returncode in command.success_exit_codes:
            if has_payload:
                result["status"] = "complete"
            else:
                result["status"] = "failed"
                result["error"] = (
                    f"GNU Wget exited with accepted status {process.returncode} but retained "
                    "no mirror files or WARC response/resource evidence"
                )
        elif has_payload:
            result["status"] = "partial"
        else:
            result["status"] = "failed"
        if process.returncode not in command.success_exit_codes:
            result["error"] = f"GNU Wget exited with status {process.returncode}"
    result["ended_at"] = _utc_now()
    if result["status"] == "complete" and result["warnings"]:
        result["status"] = "complete_with_warnings"
    _write_json(result_path, result)
    return result


def _run_wget(command: WgetCommand) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        command.argv,
        cwd=command.working_directory,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate()
    except BaseException:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        raise
    return subprocess.CompletedProcess(command.argv, process.returncode, stdout, stderr)


def _aggregate_status(results: Sequence[Mapping[str, Any]]) -> str:
    required_failures = [
        result
        for result in results
        if result["required"]
        and result["status"] not in {"complete", "complete_with_warnings"}
    ]
    if required_failures:
        if any(result["status"] == "partial" for result in required_failures):
            return "partial"
        has_content = any(_has_substantive_payload(result) for result in results)
        return "partial" if has_content else "failed"
    if any(result["status"] != "complete" for result in results):
        return "complete_with_warnings"
    return "complete"


def _collection(config: Config, collection_id: str) -> CollectionConfig:
    for collection in config.collections:
        if collection.id == collection_id:
            return collection
    raise CaptureExecutionError(f"unknown collection: {collection_id}")


def _collection_dict(collection: CollectionConfig) -> dict[str, Any]:
    return {
        "id": collection.id,
        "title": collection.title,
        "homepage": collection.homepage,
        "description": collection.description,
        "risk_note": collection.risk_note,
        "rights_note": collection.rights_note,
        "tags": list(collection.tags),
        "enabled": collection.enabled,
        "recipe_path": str(collection.recipe_path) if collection.recipe_path else None,
        "recipe_api": collection.recipe_api,
        "capture": _plain(collection.capture),
        "analysis": _plain(collection.analysis),
        "sources": [_source_dict(source) for source in collection.sources],
    }


def _source_dict(source: SourceConfig) -> dict[str, Any]:
    return {
        "id": source.id,
        "kind": source.kind,
        "title": source.title,
        "description": source.description,
        "required": source.required,
        "seeds": list(source.seeds),
        "allowed_hosts": list(source.allowed_hosts),
        "capture": _plain(source.capture),
    }


def _preserve_recipe_bundle(
    config: Config,
    collection: CollectionConfig,
    metadata_root: Path,
) -> None:
    base = collection.recipe_path.parent if collection.recipe_path else config.path.parent
    declared = tuple(
        value
        for key in ("inventory_adapter", "reader_adapter", "normalizer", "ciao_rules")
        if isinstance((value := collection.analysis.get(key)), str)
    )
    try:
        if collection.recipe_path is not None:
            bundle = scan_recipe_directory(base)
        else:
            bundle = scan_declared_assets(base, declared)
        bundle_root = metadata_root / "recipe-bundle"
        copy_bundle(bundle, bundle_root)
        source_after_copy = (
            scan_recipe_directory(base)
            if collection.recipe_path is not None
            else scan_declared_assets(base, declared)
        )
        if source_after_copy.sha256 != bundle.sha256:
            raise RecipeBundleError("recipe bundle changed while copying")
        if collection.recipe_path is not None:
            authoritative = bundle_root / "collection.toml"
            recipe_bytes = config.recipe_input_bytes[collection.recipe_path]
            if authoritative.exists() and authoritative.read_bytes() != recipe_bytes:
                raise RecipeBundleError(
                    "recipe directory collection.toml differs from the selected recipe file"
                )
            if not authoritative.exists():
                authoritative.write_bytes(recipe_bytes)
            bundle = scan_recipe_directory(bundle_root)
        _write_json(
            metadata_root / "recipe-bundle-manifest.json",
            bundle.manifest(
                recipe_api=collection.recipe_api,
                collection_id=collection.id,
            ),
        )
    except (OSError, RecipeBundleError) as exc:
        raise CaptureExecutionError(
            f"cannot preserve recipe bundle for collection {collection.id}: {exc}"
        ) from exc


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _environment_metadata(wget_version: str) -> dict[str, Any]:
    return {
        "text_preserver_version": __version__,
        "python_version": platform.python_version(),
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "hostname": socket.gethostname(),
        "process_id": os.getpid(),
        "wget_version": wget_version,
    }


def _validated_wget() -> tuple[str, str]:
    executable = shutil.which("wget")
    if executable is None:
        raise CaptureExecutionError("GNU Wget executable not found on PATH")
    try:
        version = subprocess.run(
            [executable, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        help_result = subprocess.run(
            [executable, "--help"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CaptureExecutionError(f"cannot inspect GNU Wget: {exc}") from exc
    identity = (version.stdout or version.stderr).splitlines()
    required = ("--warc-file", "--warc-cdx", "--warc-tempdir")
    if (
        version.returncode != 0
        or not identity
        or "GNU Wget" not in identity[0]
        or help_result.returncode != 0
        or any(option not in help_result.stdout for option in required)
    ):
        raise CaptureExecutionError("GNU Wget with WARC support is required")
    return str(Path(executable).resolve()), identity[0]


def _failed_source_result(
    source: SourceConfig,
    error: str,
    source_root: Path | None = None,
) -> dict[str, Any]:
    now = _utc_now()
    result: dict[str, Any] = {
        "source_id": source.id,
        "required": source.required,
        "status": "failed",
        "started_at": now,
        "ended_at": now,
        "exit_code": None,
        "downloaded_files": 0,
        "downloaded_bytes": 0,
        "payloads": _empty_payloads(),
        "warnings": [],
        "error": error,
    }
    if source_root is not None:
        if _record_payloads(result, source_root):
            result["status"] = "partial"
    return result


def _interrupted_source_result(source: SourceConfig, source_root: Path) -> dict[str, Any]:
    result = _failed_source_result(source, "", source_root)
    result["status"] = "interrupted"
    result["error"] = None
    return result


def _empty_payloads() -> dict[str, Any]:
    return {
        "mirror": {"files": 0, "bytes": 0},
        "warc": {
            "files": 0,
            "bytes": 0,
            "cdx_files": 0,
            "indexed_records": None,
            "has_response_or_resource": False,
        },
    }


def _record_payloads(result: dict[str, Any], source_root: Path) -> bool:
    payloads, warnings = _measure_payloads(source_root)
    result["payloads"] = payloads
    # Persisted/API compatibility aliases for access-mirror accounting only.
    result["downloaded_files"] = payloads["mirror"]["files"]
    result["downloaded_bytes"] = payloads["mirror"]["bytes"]
    result["warnings"].extend(warnings)
    return _has_substantive_payload(result)


def _has_substantive_payload(result: Mapping[str, Any]) -> bool:
    payloads = result.get("payloads")
    if isinstance(payloads, Mapping):
        mirror = payloads.get("mirror")
        warc = payloads.get("warc")
        return bool(
            (isinstance(mirror, Mapping) and mirror.get("files"))
            or (
                isinstance(warc, Mapping)
                and warc.get("has_response_or_resource") is True
            )
        )
    # Immutable schema-v1 captures have only these mirror counters.
    return bool(result.get("downloaded_files") or result.get("downloaded_bytes"))


def _measure_payloads(source_root: Path) -> tuple[dict[str, Any], list[str]]:
    payloads = _empty_payloads()
    mirror_files, mirror_warnings = _scan_regular_files(source_root / "mirror")
    warc_files, warc_warnings = _scan_regular_files(source_root / "warc")
    warnings = mirror_warnings + warc_warnings

    payloads["mirror"] = {
        "files": len(mirror_files),
        "bytes": sum(size for _relative, _path, size in mirror_files),
    }
    retained_warc_files = [
        item for item in warc_files if not item[0].parts or item[0].parts[0] != "tmp"
    ]
    containers = [
        item
        for item in retained_warc_files
        if item[0].name.lower().endswith((".warc", ".warc.gz"))
    ]
    cdx_files = [
        item for item in retained_warc_files if item[0].name.lower().endswith(".cdx")
    ]
    warc_payload = payloads["warc"]
    warc_payload["files"] = len(containers)
    warc_payload["bytes"] = sum(size for _relative, _path, size in containers)
    warc_payload["cdx_files"] = len(cdx_files)

    indexed_records, cdx_warnings = _indexed_record_count(cdx_files)
    warnings.extend(cdx_warnings)
    warc_payload["indexed_records"] = indexed_records
    if indexed_records is not None:
        warc_payload["has_response_or_resource"] = bool(containers and indexed_records > 0)
        if indexed_records > 0 and not containers:
            warnings.append("WARC CDX records retained without a WARC container")
    elif containers:
        evidence, inspection_warnings = _inspect_warc_containers(containers)
        warc_payload["has_response_or_resource"] = evidence
        warnings.extend(inspection_warnings)
    return payloads, warnings


def _scan_regular_files(root: Path) -> tuple[list[tuple[Path, Path, int]], list[str]]:
    files: list[tuple[Path, Path, int]] = []
    warnings: list[str] = []
    try:
        root_info = root.lstat()
    except FileNotFoundError:
        return files, warnings
    except OSError as exc:
        return files, [f"cannot inspect payload path {root}: {exc}"]
    if stat.S_ISLNK(root_info.st_mode):
        return files, [f"unsafe payload symlink ignored: {root}"]
    if not stat.S_ISDIR(root_info.st_mode):
        return files, [f"unsafe non-directory payload root ignored: {root}"]

    pending = [(root, Path())]
    while pending:
        directory, relative_directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                children = sorted(entries, key=lambda entry: entry.name, reverse=True)
        except OSError as exc:
            warnings.append(f"cannot inspect payload directory {directory}: {exc}")
            continue
        for entry in children:
            path = Path(entry.path)
            relative = relative_directory / entry.name
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                warnings.append(f"cannot inspect payload path {path}: {exc}")
                continue
            if stat.S_ISREG(info.st_mode):
                files.append((relative, path, info.st_size))
            elif stat.S_ISDIR(info.st_mode):
                pending.append((path, relative))
            elif stat.S_ISLNK(info.st_mode):
                warnings.append(f"unsafe payload symlink ignored: {path}")
            else:
                warnings.append(f"unsafe special payload file ignored: {path}")
    files.sort(key=lambda item: item[0].as_posix())
    return files, warnings


def _indexed_record_count(
    cdx_files: Sequence[tuple[Path, Path, int]],
) -> tuple[int | None, list[str]]:
    if not cdx_files:
        return None, []
    if len(cdx_files) > MAX_CDX_FILES:
        return None, [f"WARC CDX file count exceeds safety limit ({MAX_CDX_FILES})"]
    if any(size > MAX_CDX_FILE_BYTES for _relative, _path, size in cdx_files):
        return None, [f"WARC CDX file exceeds safety limit ({MAX_CDX_FILE_BYTES} bytes)"]
    if sum(size for _relative, _path, size in cdx_files) > MAX_CDX_TOTAL_BYTES:
        return None, [f"WARC CDX bytes exceed safety limit ({MAX_CDX_TOTAL_BYTES})"]

    total = 0
    warnings: list[str] = []
    for relative, path, _size in cdx_files:
        try:
            count = _count_cdx_records(path)
        except (OSError, ValueError) as exc:
            warnings.append(f"cannot inspect WARC CDX {relative.as_posix()}: {exc}")
            return None, warnings
        total += count
        if total > MAX_CDX_RECORDS:
            return None, [f"WARC CDX record count exceeds safety limit ({MAX_CDX_RECORDS})"]
    return total, warnings


def _count_cdx_records(path: Path) -> int:
    records = 0
    saw_header = False
    with _open_regular_binary(path) as (stream, size):
        if size > MAX_CDX_FILE_BYTES:
            raise ValueError(f"file exceeds safety limit ({MAX_CDX_FILE_BYTES} bytes)")
        while True:
            line = stream.readline(MAX_CDX_LINE_BYTES + 1)
            if not line:
                break
            if len(line) > MAX_CDX_LINE_BYTES:
                raise ValueError(f"line exceeds safety limit ({MAX_CDX_LINE_BYTES} bytes)")
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith(b"CDX "):
                saw_header = True
                continue
            if not saw_header:
                raise ValueError("missing GNU Wget CDX header")
            records += 1
            if records > MAX_CDX_RECORDS:
                raise ValueError(f"record count exceeds safety limit ({MAX_CDX_RECORDS})")
    if not saw_header:
        raise ValueError("missing GNU Wget CDX header")
    return records


@contextmanager
def _open_regular_binary(path: Path) -> Iterator[tuple[Any, int]]:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"unsafe non-regular file: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            yield stream, info.st_size
    finally:
        os.close(descriptor)


def _inspect_warc_containers(
    containers: Sequence[tuple[Path, Path, int]],
) -> tuple[bool, list[str]]:
    warnings: list[str] = []
    inspected_bytes = 0
    for index, (relative, path, _size) in enumerate(containers):
        if index >= MAX_WARC_INSPECTION_FILES:
            warnings.append(
                f"bounded WARC inspection stopped after {MAX_WARC_INSPECTION_FILES} files"
            )
            break
        remaining = MAX_WARC_INSPECTION_BYTES_TOTAL - inspected_bytes
        if remaining <= 0:
            warnings.append(
                f"bounded WARC inspection stopped after {MAX_WARC_INSPECTION_BYTES_TOTAL} bytes"
            )
            break
        limit = min(MAX_WARC_INSPECTION_BYTES_PER_FILE, remaining)
        try:
            data = _read_warc_prefix(path, limit)
        except (EOFError, gzip.BadGzipFile, OSError, ValueError, zlib.error) as exc:
            warnings.append(f"cannot inspect WARC {relative.as_posix()}: {exc}")
            continue
        inspected_bytes += len(data)
        if _warc_prefix_has_response_or_resource(data):
            return True, warnings
    return False, warnings


def _read_warc_prefix(path: Path, limit: int) -> bytes:
    with _open_regular_binary(path) as (stream, _size):
        if path.name.lower().endswith(".gz"):
            with gzip.GzipFile(fileobj=stream) as compressed:
                return compressed.read(limit)
        return stream.read(limit)


def _warc_prefix_has_response_or_resource(data: bytes) -> bool:
    position = 0
    while position < len(data):
        while data[position : position + 2] == b"\r\n":
            position += 2
        while data[position : position + 1] == b"\n":
            position += 1
        if not data[position:].startswith(b"WARC/"):
            return False
        crlf_end = data.find(b"\r\n\r\n", position)
        lf_end = data.find(b"\n\n", position)
        candidates = [value for value in (crlf_end, lf_end) if value >= 0]
        if not candidates:
            return False
        header_end = min(candidates)
        separator_size = 4 if header_end == crlf_end else 2
        headers: dict[bytes, bytes] = {}
        for line in data[position:header_end].splitlines()[1:]:
            name, separator, value = line.partition(b":")
            if separator:
                headers[name.strip().lower()] = value.strip().lower()
        if headers.get(b"warc-type") in {b"response", b"resource"}:
            return True
        try:
            content_length = int(headers[b"content-length"])
        except (KeyError, ValueError):
            return False
        if content_length < 0:
            return False
        position = header_end + separator_size + content_length
    return False


def _write_json(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _write_text(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")


def _update_latest(capture_directory: Path, *, source_id: str | None = None) -> bool:
    collection_root = capture_directory.parent.parent
    relative = capture_directory.relative_to(collection_root).as_posix()
    name = "LATEST-ACQUIRED" if source_id is None else f"LATEST-{source_id}"
    pointer = collection_root / name
    if pointer.is_symlink() or (pointer.exists() and not pointer.is_file()):
        raise CaptureExecutionError(f"unsafe capture pointer: {pointer}")
    if pointer.is_file():
        try:
            current_value = pointer.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            raise CaptureExecutionError(
                f"cannot read capture pointer {pointer}: {exc}"
            ) from exc
        current_relative = Path(current_value)
        if (
            len(current_relative.parts) != 2
            or current_relative.parts[0] != "captures"
            or CAPTURE_ID_RE.fullmatch(current_relative.parts[1]) is None
        ):
            raise CaptureExecutionError(f"unsafe capture pointer content: {pointer}")
        current_capture = collection_root / current_relative
        current_timestamp = current_capture.name.split("-", 1)[0]
        capture_timestamp = capture_directory.name.split("-", 1)[0]
        if (
            current_timestamp > capture_timestamp
            and _valid_pointer_target(current_capture, source_id=source_id)
        ):
            return False
    temporary = collection_root / f".{name}-{secrets.token_hex(8)}.tmp"
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600)
        try:
            os.write(descriptor, (relative + "\n").encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, pointer)
        directory_descriptor = os.open(collection_root, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def _valid_pointer_target(capture_directory: Path, *, source_id: str | None) -> bool:
    if capture_directory.is_symlink() or not capture_directory.is_dir():
        return False
    verification = verify_capture(capture_directory)
    if not verification.ok:
        return False
    try:
        capture = json.loads((capture_directory / "capture.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if (
        not isinstance(capture, dict)
        or capture.get("capture_id") != capture_directory.name
        or capture.get("collection_id") != capture_directory.parent.parent.name
    ):
        return False
    sources = capture.get("sources")
    if not isinstance(sources, list):
        return False
    if source_id is not None:
        return any(
            isinstance(result, dict)
            and result.get("source_id") == source_id
            and result.get("status") in {"complete", "complete_with_warnings"}
            for result in sources
        )
    try:
        resolved = json.loads(
            (capture_directory / "metadata/resolved-collection.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    configured_sources = resolved.get("sources") if isinstance(resolved, dict) else None
    if not isinstance(configured_sources, list):
        return False
    configured_ids = [
        item.get("id") for item in configured_sources if isinstance(item, dict)
    ]
    if len(configured_ids) != len(configured_sources) or not all(
        isinstance(value, str) for value in configured_ids
    ):
        return False
    return is_complete_full_capture(capture, configured_ids)


def _generate_capture_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{secrets.token_hex(3)}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
