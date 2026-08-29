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
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
from urllib.parse import urldefrag, urljoin, urlsplit
import zlib
from types import MappingProxyType
from typing import Any, Iterator, Mapping, Sequence

from text_preserver import __version__
from text_preserver.preservation.capture.engines.wget import (
    WgetCommand,
    build_redirect_command,
)
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


CAPTURE_MANIFEST_SCHEMA_VERSION = 3
MAX_REDIRECT_HOPS = 8
MAX_REDIRECT_VISITED = 1_000
MAX_REDIRECT_RECORDS = 100_000
MAX_REDIRECT_WARC_BYTES_PER_FILE = 64 * 1024 * 1024
MAX_REDIRECT_WARC_BYTES_TOTAL = 256 * 1024 * 1024
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
                wget,
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
    wget_executable: str,
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
        "payload_roles": [],
    }

    capture_path = plan.capture_directory / "capture.json"
    _write_json(capture_path, capture_metadata)
    results: list[dict[str, Any]] = []
    try:
        metadata_root = plan.capture_directory / "metadata"
        metadata_root.mkdir()
        private_root = metadata_root / "private"
        private_root.mkdir()
        (private_root / "input-config.toml").write_bytes(config.input_bytes)
        if collection.recipe_path is not None:
            (private_root / "input-collection-recipe.toml").write_bytes(
                config.recipe_input_bytes[collection.recipe_path]
            )
        _preserve_recipe_bundle(config, collection, metadata_root)
        _write_json(
            metadata_root / "environment.json",
            _environment_metadata(wget_version),
        )
        _write_json(
            private_root / "environment.json",
            _private_environment_metadata(config, wget_executable),
        )
        _write_json(
            private_root / "operator.json",
            {
                "operator": config.project.operator,
                "contact": config.project.contact,
                "note": operator_note,
            },
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
        _write_terminal_capture(capture_path, capture_metadata, collection)
        raise KeyboardInterrupt from exc
    except KeyboardInterrupt:
        capture_metadata["status"] = "interrupted"
        capture_metadata["ended_at"] = _utc_now()
        capture_metadata["sources"] = results
        _write_terminal_capture(capture_path, capture_metadata, collection)
        raise
    except Exception as exc:
        capture_metadata["status"] = "failed"
        capture_metadata["ended_at"] = _utc_now()
        capture_metadata["sources"] = results
        capture_metadata["error"] = str(exc)
        _write_terminal_capture(capture_path, capture_metadata, collection)
        raise CaptureExecutionError(
            f"capture failed during setup or finalization: {exc}"
        ) from exc

    capture_metadata["sources"] = results
    capture_metadata["status"] = _aggregate_status(results)
    capture_metadata["ended_at"] = _utc_now()
    _write_terminal_capture(capture_path, capture_metadata, collection)
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
    private_root = metadata_root / "private"
    private_root.mkdir()
    _write_text(source_root / "seeds.txt", "\n".join(source.seeds) + "\n")
    _write_command_provenance(metadata_root, [command])
    _write_json(metadata_root / "resolved-source.json", _source_dict(source))
    redirects_path = metadata_root / "redirects.json"
    redirect_record: dict[str, Any] = {
        "max_hops": MAX_REDIRECT_HOPS,
        "max_visited": MAX_REDIRECT_VISITED,
        "proposals": [],
    }
    _write_json(redirects_path, redirect_record)

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
        invocations, processes, redirect_warnings = _run_reviewed_redirects(
            command,
            source,
            redirects_path,
            redirect_record,
            metadata_root,
        )
        process = processes[0]
        result["exit_code"] = process.returncode
        result["warnings"].extend(redirect_warnings)
        for invocation in processes:
            if invocation.stderr.strip():
                result["warnings"].append(invocation.stderr.strip())
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
        failed_codes: list[int] = []
        for command_invocation, completed in zip(invocations, processes, strict=True):
            if completed.returncode in command.success_exit_codes:
                continue
            if _invocation_completed_by_reviewed_redirect(command_invocation, redirect_record):
                result["warnings"].append(
                    f"GNU Wget exited with status {completed.returncode} after an explicitly "
                    "reviewed redirect was followed"
                )
                continue
            failed_codes.append(completed.returncode)
        if not failed_codes:
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
        if failed_codes:
            result["error"] = "GNU Wget exited with status " + ", ".join(
                str(code) for code in failed_codes
            )
    result["ended_at"] = _utc_now()
    if result["status"] == "complete" and result["warnings"]:
        result["status"] = "complete_with_warnings"
    _write_json(result_path, result)
    return result


def _run_reviewed_redirects(
    command: WgetCommand,
    source: SourceConfig,
    redirects_path: Path,
    redirect_record: dict[str, Any],
    metadata_root: Path,
) -> tuple[list[WgetCommand], list[subprocess.CompletedProcess[str]], list[str]]:
    reviewed = set(source.reviewed_redirects)
    invocations = [command]
    processes = [_run_wget(command)]
    visited = set(command.request_urls)
    depths = {url: 0 for url in command.request_urls}
    proposals: dict[tuple[str, str | None, str], dict[str, Any]] = {}
    warnings: list[str] = []

    while True:
        discovered, response_urls, discovery_warnings = _discover_warc_redirects(
            command.working_directory
        )
        for warning in discovery_warnings:
            if warning not in warnings:
                warnings.append(warning)
        for response_url in response_urls:
            visited.add(response_url)
            depths.setdefault(response_url, 0)
        proposals_changed = False
        for source_url, location, target_url in discovered:
            key = (source_url, target_url, location)
            if key in proposals:
                continue
            proposal: dict[str, Any] = {
                "from": source_url,
                "location": location,
                "to": target_url,
                "reviewed": target_url is not None
                and (source_url, target_url) in reviewed,
                "requested": False,
            }
            proposals[key] = proposal
            proposals_changed = True
        if proposals_changed:
            redirect_record["proposals"] = list(proposals.values())
            _write_json(redirects_path, redirect_record)

        next_proposal: dict[str, Any] | None = None
        for proposal in proposals.values():
            source_url = proposal["from"]
            target_url = proposal["to"]
            depth = depths.get(source_url, 0)
            if (
                proposal["reviewed"]
                and not proposal["requested"]
                and isinstance(target_url, str)
                and target_url not in visited
                and depth < MAX_REDIRECT_HOPS
                and len(visited) < MAX_REDIRECT_VISITED
            ):
                next_proposal = proposal
                break
        if next_proposal is None:
            break

        target_url = str(next_proposal["to"])
        remaining_quota = _remaining_redirect_quota(command)
        if remaining_quota is not None and remaining_quota <= 0:
            warnings.append("reviewed redirect was not requested because the source quota is exhausted")
            break
        next_proposal["requested"] = True
        redirect_record["proposals"] = list(proposals.values())
        _write_json(redirects_path, redirect_record)
        redirect_command = build_redirect_command(
            command,
            target_url,
            len(invocations),
            remaining_quota=remaining_quota,
        )
        invocations.append(redirect_command)
        _write_command_provenance(metadata_root, invocations)
        visited.add(target_url)
        depths[target_url] = depths.get(str(next_proposal["from"]), 0) + 1
        processes.append(_run_wget(redirect_command))
    return invocations, processes, warnings


def _invocation_completed_by_reviewed_redirect(
    invocation: WgetCommand,
    redirect_record: Mapping[str, Any],
) -> bool:
    proposals = redirect_record.get("proposals")
    if not isinstance(proposals, list) or not invocation.request_urls:
        return False
    return all(
        any(
            isinstance(proposal, dict)
            and proposal.get("from") == request_url
            and proposal.get("reviewed") is True
            and proposal.get("requested") is True
            for proposal in proposals
        )
        for request_url in invocation.request_urls
    )


def _remaining_redirect_quota(command: WgetCommand) -> int | None:
    value = next(
        (argument.removeprefix("--quota=") for argument in command.argv if argument.startswith("--quota=")),
        None,
    )
    if value is None:
        return None
    match = re.fullmatch(r"([1-9][0-9]*)([KkMmGg]?)", value)
    if match is None:
        return 0
    factor = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}[match.group(2).lower()]
    quota = int(match.group(1)) * factor
    mirror_files, _warnings = _scan_regular_files(command.working_directory / "mirror")
    warc_files, _warnings = _scan_regular_files(command.working_directory / "warc")
    containers = [
        path
        for relative, path, _size in warc_files
        if (not relative.parts or relative.parts[0] != "tmp")
        and relative.name.lower().endswith((".warc", ".warc.gz"))
    ]
    try:
        retained = (
            _warc_logical_bytes(containers, quota)
            if containers
            else sum(size for _relative, _path, size in mirror_files)
        )
    except (EOFError, gzip.BadGzipFile, OSError, zlib.error):
        return 0
    return max(0, quota - retained)


def _warc_logical_bytes(paths: Sequence[Path], limit: int) -> int:
    total = 0
    for path in paths:
        if path.name.lower().endswith(".gz"):
            with gzip.open(path, "rb") as stream:
                while total < limit:
                    block = stream.read(min(1024 * 1024, limit - total))
                    if not block:
                        break
                    total += len(block)
        else:
            total += min(path.stat().st_size, limit - total)
        if total >= limit:
            return limit
    return total


def _write_command_provenance(
    metadata_root: Path,
    invocations: Sequence[WgetCommand],
) -> None:
    portable = invocations[0].to_portable_dict()
    portable["redirect_commands"] = [
        invocation.to_portable_dict() for invocation in invocations[1:]
    ]
    exact = invocations[0].to_dict()
    exact["redirect_commands"] = [invocation.to_dict() for invocation in invocations[1:]]
    _write_json(metadata_root / "command.json", portable)
    _write_json(metadata_root / "private/command.json", exact)


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
        "recipe_api": collection.recipe_api,
        "capture": _plain(collection.capture),
        "analysis": _portable_analysis(collection.analysis),
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
        "reviewed_redirects": [
            {"from": source_url, "to": target_url}
            for source_url, target_url in source.reviewed_redirects
        ],
        "capture": _plain(source.capture),
    }


def _portable_analysis(analysis: Mapping[str, Any]) -> dict[str, Any]:
    result = _plain(analysis)
    for key in (
        "inventory_adapter",
        "validator_adapter",
        "reader_adapter",
        "normalizer",
        "ciao_rules",
    ):
        value = result.get(key)
        if isinstance(value, str) and Path(value).is_absolute():
            result[key] = Path(value).name
    return result


def _preserve_recipe_bundle(
    config: Config,
    collection: CollectionConfig,
    metadata_root: Path,
) -> None:
    base = collection.recipe_path.parent if collection.recipe_path else config.path.parent
    declared = tuple(
        value
        for key in (
            "inventory_adapter",
            "validator_adapter",
            "reader_adapter",
            "normalizer",
            "ciao_rules",
        )
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
        "wget_version": wget_version,
    }


def _private_environment_metadata(
    config: Config,
    wget_executable: str,
) -> dict[str, Any]:
    return {
        "hostname": socket.gethostname(),
        "process_id": os.getpid(),
        "platform_system": platform.system(),
        "platform_machine": platform.machine(),
        "python_executable": sys.executable,
        "wget_executable": wget_executable,
        "config_path": str(config.path),
        "archive_root": str(config.project.archive_root),
        "derived_root": str(config.project.derived_root),
        "workspace_root": str(config.project.workspace_root),
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


def _discover_warc_redirects(
    source_root: Path,
) -> tuple[list[tuple[str, str, str | None]], set[str], list[str]]:
    warc_files, scan_warnings = _scan_regular_files(source_root / "warc")
    containers = [
        item
        for item in warc_files
        if (not item[0].parts or item[0].parts[0] != "tmp")
        and item[0].name.lower().endswith((".warc", ".warc.gz"))
    ]
    proposals: list[tuple[str, str, str | None]] = []
    responses: set[str] = set()
    warnings = list(scan_warnings)
    total_bytes = 0
    total_records = 0
    for relative, path, _size in containers:
        remaining = MAX_REDIRECT_WARC_BYTES_TOTAL - total_bytes
        if remaining <= 0:
            warnings.append(
                "redirect discovery stopped at the retained WARC byte limit "
                f"({MAX_REDIRECT_WARC_BYTES_TOTAL})"
            )
            break
        limit = min(MAX_REDIRECT_WARC_BYTES_PER_FILE, remaining)
        try:
            data = _read_warc_prefix(path, limit + 1)
        except (EOFError, gzip.BadGzipFile, OSError, ValueError, zlib.error) as exc:
            warnings.append(
                f"cannot inspect WARC redirects in {relative.as_posix()}: {exc}"
            )
            continue
        if len(data) > limit:
            warnings.append(
                f"redirect discovery truncated WARC {relative.as_posix()} at {limit} bytes"
            )
            data = data[:limit]
        total_bytes += len(data)
        file_proposals, file_responses, records = _redirects_from_warc_bytes(
            data,
            MAX_REDIRECT_RECORDS - total_records,
        )
        proposals.extend(file_proposals)
        responses.update(file_responses)
        total_records += records
        if total_records >= MAX_REDIRECT_RECORDS:
            warnings.append(
                f"redirect discovery stopped at the WARC record limit ({MAX_REDIRECT_RECORDS})"
            )
            break
    return proposals, responses, warnings


def _redirects_from_warc_bytes(
    data: bytes,
    record_limit: int,
) -> tuple[list[tuple[str, str, str | None]], set[str], int]:
    proposals: list[tuple[str, str, str | None]] = []
    responses: set[str] = set()
    position = 0
    records = 0
    while position < len(data) and records < record_limit:
        while data[position : position + 2] == b"\r\n":
            position += 2
        while data[position : position + 1] == b"\n":
            position += 1
        if not data[position:].startswith(b"WARC/"):
            break
        header_end, separator_size = _header_end(data, position)
        if header_end is None:
            break
        headers = _header_fields(data[position:header_end])
        try:
            content_length = int(headers[b"content-length"])
        except (KeyError, ValueError):
            break
        payload_start = header_end + separator_size
        payload_end = payload_start + content_length
        if content_length < 0 or payload_end > len(data):
            break
        records += 1
        target_bytes = headers.get(b"warc-target-uri")
        if headers.get(b"warc-type", b"").lower() == b"response" and target_bytes:
            try:
                response_url = target_bytes.decode("utf-8")
            except UnicodeDecodeError:
                response_url = ""
            if response_url.startswith("<") and response_url.endswith(">"):
                response_url = response_url[1:-1]
            if _safe_observed_http_url(response_url):
                responses.add(response_url)
                proposal = _redirect_from_http_payload(
                    response_url,
                    data[payload_start:payload_end],
                )
                if proposal is not None:
                    proposals.append(proposal)
        position = payload_end
    return proposals, responses, records


def _redirect_from_http_payload(
    source_url: str,
    payload: bytes,
) -> tuple[str, str, str | None] | None:
    header_end, _separator_size = _header_end(payload, 0)
    if header_end is None:
        return None
    lines = payload[:header_end].splitlines()
    if not lines:
        return None
    status = lines[0].split()
    if len(status) < 2 or not status[0].startswith(b"HTTP/"):
        return None
    try:
        status_code = int(status[1])
    except ValueError:
        return None
    if not 300 <= status_code <= 399:
        return None
    headers = _header_fields(payload[:header_end])
    location_bytes = headers.get(b"location")
    if location_bytes is None:
        return None
    location = location_bytes.decode("latin-1").strip()
    resolved_target, _fragment = urldefrag(urljoin(source_url, location))
    target_url: str | None = resolved_target
    if not _safe_observed_http_url(resolved_target):
        target_url = None
    return source_url, location, target_url


def _header_end(data: bytes, position: int) -> tuple[int | None, int]:
    crlf_end = data.find(b"\r\n\r\n", position)
    lf_end = data.find(b"\n\n", position)
    candidates = [
        (value, 4 if value == crlf_end else 2)
        for value in (crlf_end, lf_end)
        if value >= 0
    ]
    return min(candidates) if candidates else (None, 0)


def _header_fields(block: bytes) -> dict[bytes, bytes]:
    headers: dict[bytes, bytes] = {}
    for line in block.splitlines()[1:]:
        name, separator, value = line.partition(b":")
        if separator:
            headers[name.strip().lower()] = value.strip()
    return headers


def _safe_observed_http_url(value: str) -> bool:
    if "\\" in value or any(
        character.isspace() or ord(character) == 127 for character in value
    ):
        return False
    try:
        parts = urlsplit(value)
        _ = parts.port
        hostname = parts.hostname or ""
        hostname.encode("ascii")
    except (UnicodeEncodeError, ValueError):
        return False
    return bool(
        parts.scheme in {"http", "https"}
        and hostname
        and hostname == hostname.lower()
        and parts.username is None
        and parts.password is None
        and not parts.fragment
    )


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


def _write_terminal_capture(
    capture_path: Path,
    metadata: dict[str, Any],
    collection: CollectionConfig,
) -> None:
    source_kinds = {source.id: source.kind for source in collection.sources}
    paths = {
        path.relative_to(capture_path.parent).as_posix()
        for directory, _directory_names, file_names in os.walk(capture_path.parent)
        for name in file_names
        if stat.S_ISREG((path := Path(directory) / name).lstat().st_mode)
    }
    paths.update({"capture.json", "SHA256SUMS", "manifest-sha256.json"})
    metadata["payload_roles"] = [
        {
            "path": path,
            "role": _capture_payload_role(path, source_kinds),
        }
        for path in sorted(paths)
    ]
    _write_json(capture_path, metadata)


def _capture_payload_role(path: str, source_kinds: Mapping[str, str]) -> str:
    parts = Path(path).parts
    if len(parts) >= 4 and parts[0] == "sources":
        if parts[2] == "warc":
            if path.lower().endswith((".warc", ".warc.gz")):
                return "preservation_original"
            return "capture_derivative"
        if parts[2] == "mirror":
            if source_kinds.get(parts[1]) == "http-file":
                return "preservation_original"
            return "capture_derivative"
    return "metadata"


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
    return is_complete_full_capture(capture, (str(value) for value in configured_ids))


def _generate_capture_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{secrets.token_hex(3)}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
