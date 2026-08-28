"""Execute capture plans while preserving provenance and failures."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import fcntl
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
from types import MappingProxyType
from typing import Any, Iterator, Mapping, Sequence

from text_preserver import __version__
from text_preserver.capture.engines.wget import WgetCommand
from text_preserver.capture.plan import CAPTURE_ID_RE, CapturePlan, plan_capture
from text_preserver.config import CollectionConfig, Config, SourceConfig
from text_preserver.manifest import ManifestError, finalize_capture, verify_capture


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
        "schema_version": 1,
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
        _preserve_recipe_assets(config, collection, metadata_root)
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
            except OSError as exc:
                result = _failed_source_result(source, str(exc))
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
        result["ended_at"] = _utc_now()
        _write_json(result_path, result)
        raise _SourceInterrupted(result)
    except OSError as exc:
        result["status"] = "failed"
        result["error"] = str(exc)
    else:
        files, size = _download_counts(source_root / "mirror")
        result["downloaded_files"] = files
        result["downloaded_bytes"] = size
        if process.returncode in command.success_exit_codes:
            result["status"] = "complete"
        elif files:
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
        has_content = any(
            result["downloaded_files"] or result["downloaded_bytes"] for result in results
        )
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


def _preserve_recipe_assets(
    config: Config,
    collection: CollectionConfig,
    metadata_root: Path,
) -> None:
    base = collection.recipe_path.parent if collection.recipe_path else config.path.parent
    assets_root = metadata_root / "recipe-assets"
    for key in ("inventory_adapter", "normalizer", "ciao_rules"):
        value = collection.analysis.get(key)
        if not isinstance(value, str):
            continue
        relative = Path(value)
        if relative.is_absolute() or ".." in relative.parts:
            raise CaptureExecutionError(
                f"collection {collection.id} analysis asset must be recipe-relative: {value}"
            )
        source = (base / relative).resolve()
        if not source.is_relative_to(base.resolve()) or source.is_symlink() or not source.is_file():
            raise CaptureExecutionError(
                f"collection {collection.id} analysis asset is not a regular recipe file: {source}"
            )
        target = assets_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())


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


def _failed_source_result(source: SourceConfig, error: str) -> dict[str, Any]:
    now = _utc_now()
    return {
        "source_id": source.id,
        "required": source.required,
        "status": "failed",
        "started_at": now,
        "ended_at": now,
        "exit_code": None,
        "downloaded_files": 0,
        "downloaded_bytes": 0,
        "warnings": [],
        "error": error,
    }


def _download_counts(root: Path) -> tuple[int, int]:
    if not root.exists():
        return 0, 0
    files = [path for path in root.rglob("*") if path.is_file()]
    return len(files), sum(path.stat().st_size for path in files)


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
    name = "LATEST" if source_id is None else f"LATEST-{source_id}"
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
    if capture.get("status") != "complete":
        return False
    try:
        resolved = json.loads(
            (capture_directory / "metadata/resolved-collection.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    configured_sources = resolved.get("sources") if isinstance(resolved, dict) else None
    selected_sources = capture.get("selected_sources")
    if not isinstance(configured_sources, list) or not isinstance(selected_sources, list):
        return False
    configured_ids = [
        item.get("id") for item in configured_sources if isinstance(item, dict)
    ]
    if (
        len(configured_ids) != len(configured_sources)
        or not all(isinstance(value, str) for value in configured_ids)
        or not all(isinstance(value, str) for value in selected_sources)
    ):
        return False
    return set(configured_ids) == set(selected_sources) and len(configured_ids) == len(
        selected_sources
    )


def _generate_capture_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{secrets.token_hex(3)}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
