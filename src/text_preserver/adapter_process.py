"""Run recipe adapters in a bounded, disposable subprocess.

This module deliberately does not import adapter code.  It owns the process and
protocol boundary; :mod:`text_preserver.adapter_worker` is the only interpreter
which loads an adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Mapping, Sequence
import uuid

from text_preserver.preservation.recipe_bundle import (
    RecipeBundleError,
    copy_bundle,
    scan_declared_assets,
    scan_recipe_directory,
)


PROTOCOL_NAME = "text-preserver.adapter"
PROTOCOL_VERSION = 1
EXECUTION_POLICY_VERSION = 1
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_OPERATION_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_MAX_OUTPUT_FILES = 2_000
_MAX_OUTPUT_DIRECTORIES = 2_000
_MAX_OUTPUT_BYTES = 512 * 1024 * 1024


class AdapterProcessError(ValueError):
    """Raised when an invocation cannot safely be constructed."""


@dataclass(frozen=True)
class AdapterLimits:
    """Hard and transport limits for one adapter invocation."""

    wall_seconds: float = 30.0
    memory_bytes: int = 1024 * 1024 * 1024
    file_size_bytes: int = 256 * 1024 * 1024
    max_adapter_bytes: int = 8 * 1024 * 1024
    max_request_bytes: int = 1024 * 1024
    max_protocol_bytes: int = 8 * 1024 * 1024
    max_diagnostic_bytes: int = 64 * 1024

    def validate(self) -> None:
        integer_fields = (
            "memory_bytes",
            "file_size_bytes",
            "max_adapter_bytes",
            "max_request_bytes",
            "max_protocol_bytes",
            "max_diagnostic_bytes",
        )
        if not isinstance(self.wall_seconds, (int, float)) or isinstance(
            self.wall_seconds, bool
        ) or not 0 < self.wall_seconds <= 3600:
            raise AdapterProcessError("wall_seconds must be between 0 and 3600")
        for name in integer_fields:
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise AdapterProcessError(f"{name} must be a positive integer")
        if self.max_request_bytes > 8 * 1024 * 1024:
            raise AdapterProcessError("max_request_bytes must not exceed 8388608")


@dataclass(frozen=True)
class AdapterProcessResult:
    """A normal, bounded outcome from the adapter process boundary."""

    request_id: str
    ok: bool
    result: Any
    error: dict[str, str] | None
    diagnostics: dict[str, Any]
    controls: dict[str, Any]

    def envelope(self) -> dict[str, Any]:
        return {
            "protocol": PROTOCOL_NAME,
            "version": PROTOCOL_VERSION,
            "kind": "response",
            "request_id": self.request_id,
            "ok": self.ok,
            "result": self.result,
            "error": self.error,
            "diagnostics": self.diagnostics,
            "controls": self.controls,
        }


def execution_policy_identity(
    operation: str,
    limits: AdapterLimits | None = None,
) -> dict[str, Any]:
    """Return the deterministic policy identity used by derived artifacts."""
    actual_limits = limits or AdapterLimits()
    actual_limits.validate()
    filesystem_scope = (
        "dedicated_reader_output" if operation in {"reader", "stream"} else "none"
    )
    return {
        "version": EXECUTION_POLICY_VERSION,
        "protocol": {"name": PROTOCOL_NAME, "version": PROTOCOL_VERSION},
        "process": "dedicated_interpreter_subprocess",
        "runtime_policy": {
            "scope": "language-runtime policy",
            "network": "denied",
            "subprocess": "denied",
            "filesystem_write_scope": filesystem_scope,
            "full_os_sandbox": False,
        },
        "limits": {
            "wall_seconds": actual_limits.wall_seconds,
            "memory_bytes": actual_limits.memory_bytes,
            "file_size_bytes": actual_limits.file_size_bytes,
            "max_adapter_bytes": actual_limits.max_adapter_bytes,
            "max_request_bytes": actual_limits.max_request_bytes,
            "max_protocol_bytes": actual_limits.max_protocol_bytes,
            "max_diagnostic_bytes": actual_limits.max_diagnostic_bytes,
        },
    }


def adapter_digest(path: Path, *, max_bytes: int = 8 * 1024 * 1024) -> str:
    """Hash a regular, non-symlink adapter without importing it."""
    if type(max_bytes) is not int or max_bytes <= 0:
        raise AdapterProcessError("max_bytes must be a positive integer")
    resolved, descriptor = _open_adapter(path, max_bytes)
    del resolved
    digest = hashlib.sha256()
    try:
        while block := os.read(descriptor, 1024 * 1024):
            digest.update(block)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def invoke_adapter(
    adapter_path: Path,
    *,
    adapter_sha256: str,
    recipe_api: int,
    operation: str,
    context: Mapping[str, Any],
    arguments: Mapping[str, Any] | None = None,
    limits: AdapterLimits | None = None,
    allow_python_network: bool = False,
    allow_python_subprocess: bool = False,
    bundle_root: str | Path | None = None,
    bundle_sha256: str | None = None,
    bundle_paths: Sequence[str] | None = None,
) -> AdapterProcessResult:
    """Invoke an adapter through the version-1 JSON subprocess protocol.

    API 1 operations are ``validate``, ``render``, ``stream``, and the
    production ``reader`` dispatch. API 2 supports typed production entry
    points as well as the generic ``handle_operation`` extension point.
    """
    actual_limits = limits or AdapterLimits()
    actual_limits.validate()
    if type(recipe_api) is not int or recipe_api not in {1, 2}:
        raise AdapterProcessError("recipe_api must be 1 or 2")
    if not isinstance(operation, str) or _OPERATION_RE.fullmatch(operation) is None:
        raise AdapterProcessError("operation must be a lowercase JSON operation name")
    if recipe_api == 1 and operation not in {"validate", "render", "stream", "reader"}:
        raise AdapterProcessError(f"unsupported recipe API 1 operation: {operation}")
    if not isinstance(adapter_sha256, str) or _DIGEST_RE.fullmatch(adapter_sha256) is None:
        raise AdapterProcessError("adapter_sha256 must be a lowercase SHA-256 digest")
    if not isinstance(context, Mapping) or not isinstance(arguments or {}, Mapping):
        raise AdapterProcessError("context and arguments must be JSON objects")
    if type(allow_python_network) is not bool or type(allow_python_subprocess) is not bool:
        raise AdapterProcessError("adapter runtime policy values must be booleans")

    resolved, descriptor = _open_adapter(adapter_path, actual_limits.max_adapter_bytes)
    try:
        digest = hashlib.sha256()
        while block := os.read(descriptor, 1024 * 1024):
            digest.update(block)
    finally:
        os.close(descriptor)
    if digest.hexdigest() != adapter_sha256:
        raise AdapterProcessError(
            f"adapter digest does not match expected SHA-256: {resolved}"
        )

    normalized_context = _normalize_context(recipe_api, operation, context)
    request_id = uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="text-preserver-adapter-") as temporary:
        worker_adapter = resolved
        if bundle_paths is not None and bundle_root is None:
            raise AdapterProcessError("bundle_paths requires bundle_root")
        if bundle_root is not None or bundle_sha256 is not None:
            if bundle_root is None or not isinstance(bundle_sha256, str):
                raise AdapterProcessError("bundle_root and bundle_sha256 must be provided together")
            source_root = Path(bundle_root).resolve()
            try:
                relative_adapter = resolved.relative_to(source_root)
                bundle = (
                    scan_declared_assets(source_root, bundle_paths)
                    if bundle_paths is not None
                    else scan_recipe_directory(source_root)
                )
                if bundle.sha256 != bundle_sha256:
                    raise AdapterProcessError("recipe bundle changed before adapter execution")
                snapshot = Path(temporary) / "recipe"
                copy_bundle(bundle, snapshot)
                worker_adapter = snapshot / relative_adapter
            except (OSError, ValueError, RecipeBundleError) as exc:
                if isinstance(exc, AdapterProcessError):
                    raise
                raise AdapterProcessError(f"cannot snapshot recipe bundle: {exc}") from exc
        diagnostic_stdout = Path(temporary) / "adapter.stdout"
        diagnostic_stderr = Path(temporary) / "adapter.stderr"
        request = {
            "protocol": PROTOCOL_NAME,
            "version": PROTOCOL_VERSION,
            "kind": "request",
            "request_id": request_id,
            "adapter": {
                "path": str(worker_adapter),
                "sha256": adapter_sha256,
                "recipe_api": recipe_api,
            },
            "operation": {
                "name": operation,
                "context": normalized_context,
                "arguments": dict(arguments or {}),
            },
            "limits": {
                "memory_bytes": actual_limits.memory_bytes,
                "file_size_bytes": actual_limits.file_size_bytes,
                "max_adapter_bytes": actual_limits.max_adapter_bytes,
                "max_protocol_bytes": actual_limits.max_protocol_bytes,
                "max_diagnostic_bytes": actual_limits.max_diagnostic_bytes,
            },
            "policy": {
                "allow_python_network": allow_python_network,
                "allow_python_subprocess": allow_python_subprocess,
                "filesystem_write_scope": (
                    "reader_output" if operation in {"reader", "stream"} else "none"
                ),
                "filesystem_write_root": normalized_context.get("output_directory"),
            },
            "diagnostic_paths": {
                "stdout": str(diagnostic_stdout),
                "stderr": str(diagnostic_stderr),
            },
        }
        payload = _json_bytes(request)
        if len(payload) > actual_limits.max_request_bytes:
            raise AdapterProcessError(
                f"adapter request exceeds {actual_limits.max_request_bytes} bytes"
            )
        return _run_worker(
            payload,
            request_id,
            actual_limits,
            pycache_directory=Path(temporary) / "pycache",
        )


# A short name is useful to callers which already describe the operation in args.
run_adapter = invoke_adapter


def _normalize_context(
    recipe_api: int,
    operation: str,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    value = dict(context)
    if recipe_api == 1:
        expected = (
            {"capture_directory", "output_directory"}
            if operation in {"stream", "reader"}
            else {"capture_directory"}
        )
        if set(value) != expected:
            raise AdapterProcessError(
                f"recipe API 1 {operation} context must contain exactly: "
                + ", ".join(sorted(expected))
            )
        for key in expected:
            raw_path = value[key]
            if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
                raise AdapterProcessError(f"{key} must be an absolute path")
            value[key] = str(Path(raw_path).resolve())
        if operation in {"stream", "reader"}:
            output = Path(value["output_directory"])
            if output.is_symlink() or not output.is_dir():
                raise AdapterProcessError("output_directory must be a regular directory")
            try:
                next(output.iterdir())
            except StopIteration:
                pass
            else:
                raise AdapterProcessError("output_directory must be dedicated and empty")
    elif operation == "validate":
        expected = {
            "capture_directory",
            "expected_work_count",
            "required_representation_kinds",
            "required_source_ids",
        }
        if set(value) != expected:
            raise AdapterProcessError("recipe API 2 validation context is invalid")
        _normalize_absolute_path(value, "capture_directory")
    elif operation == "reader":
        expected = {"capture_directory", "output_directory", "expected_work_count"}
        if set(value) != expected:
            raise AdapterProcessError("recipe API 2 reader context is invalid")
        _normalize_absolute_path(value, "capture_directory")
        _normalize_absolute_path(value, "output_directory")
        output = Path(value["output_directory"])
        if output.is_symlink() or not output.is_dir():
            raise AdapterProcessError("output_directory must be a regular directory")
        try:
            next(output.iterdir())
        except StopIteration:
            pass
        else:
            raise AdapterProcessError("output_directory must be dedicated and empty")
    _json_bytes(value)
    return value


def _normalize_absolute_path(value: dict[str, Any], key: str) -> None:
    raw_path = value[key]
    if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
        raise AdapterProcessError(f"{key} must be an absolute path")
    value[key] = str(Path(raw_path).resolve())


def _open_adapter(path: Path, max_bytes: int) -> tuple[Path, int]:
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise AdapterProcessError(f"adapter must not be a symlink: {candidate}")
    resolved = candidate.resolve()
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(resolved, flags)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise AdapterProcessError(f"adapter is not a regular file: {resolved}")
        if info.st_size > max_bytes:
            raise AdapterProcessError(f"adapter exceeds {max_bytes} bytes: {resolved}")
    except Exception:
        if "descriptor" in locals():
            os.close(descriptor)
        raise
    return resolved, descriptor


def _json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AdapterProcessError(f"adapter request is not strict JSON: {exc}") from exc


def _run_worker(
    payload: bytes,
    request_id: str,
    limits: AdapterLimits,
    *,
    pycache_directory: Path,
) -> AdapterProcessResult:
    process_group_available = os.name == "posix"
    popen_options: dict[str, Any] = {}
    if os.name == "posix":
        popen_options["start_new_session"] = True
    elif os.name == "nt":  # pragma: no cover - exercised on Windows CI only
        popen_options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP")
    try:
        environment = os.environ.copy()
        environment["PYTHONPYCACHEPREFIX"] = str(pycache_directory)
        process = subprocess.Popen(
            [sys.executable, "-m", "text_preserver.adapter_worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            **popen_options,
        )
    except OSError as exc:
        return _parent_failure(
            request_id,
            "worker_start_failed",
            str(exc),
            limits,
            process_group_available,
            False,
            b"",
            False,
        )

    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    stdout = _BoundedDrain(process.stdout, limits.max_protocol_bytes)
    stderr = _BoundedDrain(process.stderr, limits.max_diagnostic_bytes)
    stdout.start()
    stderr.start()
    started = time.monotonic()
    killed_group = False
    error_code: str | None = None
    error_message = ""
    output_directory = _output_directory(payload)
    last_output_check = 0.0
    try:
        process.stdin.write(payload)
        process.stdin.close()
        while process.poll() is None:
            if stdout.exceeded.is_set():
                error_code = "protocol_output_limit"
                error_message = (
                    f"worker protocol output exceeded {limits.max_protocol_bytes} bytes"
                )
                killed_group = _terminate_process_group(process) or killed_group
                break
            if time.monotonic() - started >= limits.wall_seconds:
                error_code = "timeout"
                error_message = f"adapter exceeded {limits.wall_seconds:g} second wall timeout"
                killed_group = _terminate_process_group(process) or killed_group
                break
            if output_directory is not None and time.monotonic() - last_output_check >= 0.05:
                last_output_check = time.monotonic()
                output_error = _output_limit_error(output_directory)
                if output_error is not None:
                    error_code = "adapter_output_limit"
                    error_message = output_error
                    killed_group = _terminate_process_group(process) or killed_group
                    break
            time.sleep(0.01)
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            killed_group = _terminate_process_group(process) or killed_group
            process.wait(timeout=1.0)
    except (BrokenPipeError, OSError) as exc:
        error_code = "worker_io_failed"
        error_message = str(exc)
        killed_group = _terminate_process_group(process) or killed_group
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass

    # A successful adapter may still have left descendants behind.  Its isolated
    # POSIX session is disposable, so terminate any group which still exists.
    if os.name == "posix":
        killed_group = _terminate_process_group(process) or killed_group
    stdout.join(timeout=0.5)
    stderr.join(timeout=0.5)
    if stdout.is_alive() or stderr.is_alive():
        killed_group = _terminate_process_group(process) or killed_group
        stdout.join(timeout=0.5)
        stderr.join(timeout=0.5)
    raw_stdout = stdout.value()
    raw_stderr = stderr.value()
    if error_code is None and output_directory is not None:
        output_error = _output_limit_error(output_directory)
        if output_error is not None:
            error_code = "adapter_output_limit"
            error_message = output_error
    if error_code is not None:
        return _parent_failure(
            request_id,
            error_code,
            error_message,
            limits,
            process_group_available,
            killed_group,
            raw_stderr,
            stderr.truncated,
        )
    if stdout.truncated:
        return _parent_failure(
            request_id,
            "protocol_output_limit",
            f"worker protocol output exceeded {limits.max_protocol_bytes} bytes",
            limits,
            process_group_available,
            killed_group,
            raw_stderr,
            stderr.truncated,
        )
    if process.returncode != 0:
        return _parent_failure(
            request_id,
            "worker_failed",
            f"adapter worker exited with status {process.returncode}",
            limits,
            process_group_available,
            killed_group,
            raw_stderr,
            stderr.truncated,
        )
    try:
        response = _strict_json_loads(raw_stdout)
        _validate_response(response, request_id)
    except (UnicodeError, json.JSONDecodeError, AdapterProcessError) as exc:
        return _parent_failure(
            request_id,
            "invalid_protocol",
            f"invalid adapter worker response: {exc}",
            limits,
            process_group_available,
            killed_group,
            raw_stderr,
            stderr.truncated,
        )
    controls = _parent_controls(
        limits,
        process_group_available,
        killed_group,
        response["controls"],
    )
    diagnostics = response["diagnostics"]
    if raw_stderr:
        diagnostics = dict(diagnostics)
        diagnostics["worker_stderr"] = raw_stderr.decode("utf-8", "replace")
        diagnostics["worker_stderr_truncated"] = stderr.truncated
    return AdapterProcessResult(
        request_id=request_id,
        ok=response["ok"],
        result=response["result"],
        error=response["error"],
        diagnostics=diagnostics,
        controls=controls,
    )


def _output_directory(payload: bytes) -> Path | None:
    try:
        request = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError):
        return None
    context = request.get("operation", {}).get("context") if isinstance(request, dict) else None
    value = context.get("output_directory") if isinstance(context, dict) else None
    return Path(value) if isinstance(value, str) else None


def _output_limit_error(root: Path) -> str | None:
    files = 0
    directories = 0
    total_bytes = 0
    try:
        for directory, directory_names, file_names in os.walk(root, followlinks=False):
            directories += len(directory_names)
            if directories > _MAX_OUTPUT_DIRECTORIES:
                return f"adapter output exceeds {_MAX_OUTPUT_DIRECTORIES} directories"
            files += len(file_names)
            if files > _MAX_OUTPUT_FILES:
                return f"adapter output exceeds {_MAX_OUTPUT_FILES} files"
            base = Path(directory)
            for name in file_names:
                info = (base / name).lstat()
                if not stat.S_ISREG(info.st_mode):
                    return "adapter output contains a non-regular file"
                total_bytes += info.st_size
                if total_bytes > _MAX_OUTPUT_BYTES:
                    return f"adapter output exceeds {_MAX_OUTPUT_BYTES} bytes"
    except OSError as exc:
        return f"cannot inspect adapter output: {exc}"
    return None


class _BoundedDrain(threading.Thread):
    def __init__(self, stream: Any, limit: int) -> None:
        super().__init__(daemon=True)
        self.stream = stream
        self.limit = limit
        self.buffer = bytearray()
        self.truncated = False
        self.exceeded = threading.Event()

    def run(self) -> None:
        try:
            while block := self.stream.read(8192):
                remaining = self.limit - len(self.buffer)
                if remaining > 0:
                    self.buffer.extend(block[:remaining])
                if len(block) > remaining:
                    self.truncated = True
                    self.exceeded.set()
        except OSError:
            pass
        finally:
            self.stream.close()

    def value(self) -> bytes:
        return bytes(self.buffer)


def _terminate_process_group(process: subprocess.Popen[bytes]) -> bool:
    if process.poll() is not None and os.name != "posix":
        return False
    used_group = False
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
            used_group = True
        else:  # pragma: no cover - exercised on Windows CI only
            process.terminate()
    except ProcessLookupError:
        return used_group
    except OSError:
        process.kill()
        return used_group
    try:
        process.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
                used_group = True
            else:  # pragma: no cover
                process.kill()
        except ProcessLookupError:
            pass
    return used_group


def _validate_response(value: Any, request_id: str) -> None:
    expected = {
        "protocol",
        "version",
        "kind",
        "request_id",
        "ok",
        "result",
        "error",
        "diagnostics",
        "controls",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise AdapterProcessError("response envelope has unknown or missing fields")
    if (
        value["protocol"] != PROTOCOL_NAME
        or value["version"] != PROTOCOL_VERSION
        or value["kind"] != "response"
        or value["request_id"] != request_id
        or type(value["ok"]) is not bool
        or not isinstance(value["diagnostics"], dict)
        or not isinstance(value["controls"], dict)
    ):
        raise AdapterProcessError("response envelope identity or types are invalid")
    if value["ok"]:
        if value["error"] is not None:
            raise AdapterProcessError("successful response contains an error")
    elif (
        not isinstance(value["error"], dict)
        or set(value["error"]) != {"code", "message"}
        or not all(isinstance(item, str) for item in value["error"].values())
    ):
        raise AdapterProcessError("failed response has an invalid error")
    if not value["ok"] and value["result"] is not None:
        raise AdapterProcessError("failed response contains a result")
    if set(value["diagnostics"]) != {
        "stdout",
        "stderr",
        "stdout_truncated",
        "stderr_truncated",
    } or not (
        isinstance(value["diagnostics"]["stdout"], str)
        and isinstance(value["diagnostics"]["stderr"], str)
        and type(value["diagnostics"]["stdout_truncated"]) is bool
        and type(value["diagnostics"]["stderr_truncated"]) is bool
    ):
        raise AdapterProcessError("response diagnostics are invalid")


def _strict_json_loads(payload: bytes) -> Any:
    def reject_constant(value: str) -> Any:
        raise ValueError(f"non-finite JSON number: {value}")

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON object key: {key}")
            result[key] = value
        return result

    try:
        return json.loads(
            payload,
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicate_keys,
        )
    except ValueError as exc:
        raise AdapterProcessError(str(exc)) from exc


def _parent_failure(
    request_id: str,
    code: str,
    message: str,
    limits: AdapterLimits,
    process_group_available: bool,
    killed_group: bool,
    worker_stderr: bytes,
    stderr_truncated: bool,
) -> AdapterProcessResult:
    return AdapterProcessResult(
        request_id=request_id,
        ok=False,
        result=None,
        error={"code": code, "message": message[:2048]},
        diagnostics={
            "stdout": "",
            "stderr": "",
            "stdout_truncated": False,
            "stderr_truncated": False,
            "worker_stderr": worker_stderr.decode("utf-8", "replace"),
            "worker_stderr_truncated": stderr_truncated,
        },
        controls=_parent_controls(
            limits,
            process_group_available,
            killed_group,
            None,
        ),
    )


def _parent_controls(
    limits: AdapterLimits,
    process_group_available: bool,
    killed_group: bool,
    worker_controls: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "process_separation": {
            "enforced": True,
            "mechanism": "dedicated interpreter subprocess",
        },
        "wall_timeout": {
            "enforced": True,
            "mechanism": "parent monotonic deadline",
            "seconds": limits.wall_seconds,
        },
        "process_group_termination": {
            "available": process_group_available,
            "used": killed_group,
        },
        "worker": worker_controls
        if worker_controls is not None
        else {
            "reported": False,
            "reason": "worker did not return a valid control report",
        },
        "full_os_sandbox": {
            "enforced": False,
            "reason": "process separation and Python-runtime policy are not a full OS sandbox",
        },
    }
