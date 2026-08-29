"""Private JSON worker for :mod:`text_preserver.adapter_process`."""

from __future__ import annotations

import hashlib
import importlib
from importlib.machinery import ModuleSpec
import builtins
import io
import json
import os
from pathlib import Path
import re
import signal
import stat
import sys
from types import ModuleType
from typing import Any, Mapping


PROTOCOL_NAME = "text-preserver.adapter"
PROTOCOL_VERSION = 1
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_ID_RE = re.compile(r"[0-9a-f]{32}")
_OPERATION_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")


class ProtocolError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def main() -> int:
    protocol_stream = os.fdopen(os.dup(sys.stdout.fileno()), "wb", closefd=True)
    request: dict[str, Any] | None = None
    request_id = "0" * 32
    max_protocol_bytes = 8 * 1024 * 1024
    try:
        raw_request = sys.stdin.buffer.read(8 * 1024 * 1024 + 1)
        if len(raw_request) > 8 * 1024 * 1024:
            raise ProtocolError("request_limit", "request exceeds worker input limit")
        try:
            request = _strict_json_loads(raw_request)
        except (UnicodeError, ValueError) as exc:
            raise ProtocolError("invalid_request", f"request is not valid JSON: {exc}") from exc
        _validate_request(request)
        request_id = request["request_id"]
        max_protocol_bytes = request["limits"]["max_protocol_bytes"]
        _redirect_diagnostics(request["diagnostic_paths"])
        controls = _apply_controls(request["limits"], request["policy"])
        try:
            result = _invoke(request)
            response = _response(request_id, True, result, None, controls, request)
        except ProtocolError as exc:
            response = _response(
                request_id,
                False,
                None,
                {"code": exc.code, "message": str(exc)[:2048]},
                controls,
                request,
            )
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                message = f"adapter attempted interpreter exit: {exc}"
            else:
                message = f"{type(exc).__name__}: {exc}"
            response = _response(
                request_id,
                False,
                None,
                {"code": "adapter_failed", "message": message[:2048]},
                controls,
                request,
            )
    except ProtocolError as exc:
        response = _response(
            request_id,
            False,
            None,
            {"code": exc.code, "message": str(exc)[:2048]},
            _unavailable_controls(),
            request,
        )
    except BaseException as exc:
        response = _response(
            request_id,
            False,
            None,
            {"code": "worker_error", "message": f"{type(exc).__name__}: {exc}"[:2048]},
            _unavailable_controls(),
            request,
        )

    try:
        payload = _encode(response)
    except (TypeError, ValueError) as exc:
        response = _response(
            request_id,
            False,
            None,
            {"code": "invalid_result", "message": f"adapter result is not strict JSON: {exc}"},
            response["controls"],
            request,
        )
        payload = _encode(response)
    if len(payload) > max_protocol_bytes:
        response = _response(
            request_id,
            False,
            None,
            {
                "code": "protocol_output_limit",
                "message": f"response exceeds {max_protocol_bytes} bytes",
            },
            response["controls"],
            request,
            include_diagnostics=False,
        )
        payload = _encode(response)
    if len(payload) > max_protocol_bytes:
        return 70
    protocol_stream.write(payload)
    protocol_stream.flush()
    return 0


def _validate_request(value: Any) -> None:
    top = {
        "protocol",
        "version",
        "kind",
        "request_id",
        "adapter",
        "operation",
        "limits",
        "policy",
        "diagnostic_paths",
    }
    if not isinstance(value, dict) or set(value) != top:
        raise ProtocolError("invalid_request", "request envelope has unknown or missing fields")
    if (
        value["protocol"] != PROTOCOL_NAME
        or value["version"] != PROTOCOL_VERSION
        or value["kind"] != "request"
        or not isinstance(value["request_id"], str)
        or _ID_RE.fullmatch(value["request_id"]) is None
    ):
        raise ProtocolError("invalid_request", "request envelope identity is invalid")
    adapter = value["adapter"]
    if not isinstance(adapter, dict) or set(adapter) != {"path", "sha256", "recipe_api"}:
        raise ProtocolError("invalid_request", "adapter descriptor is invalid")
    if (
        not isinstance(adapter["path"], str)
        or not Path(adapter["path"]).is_absolute()
        or not isinstance(adapter["sha256"], str)
        or _DIGEST_RE.fullmatch(adapter["sha256"]) is None
        or type(adapter["recipe_api"]) is not int
        or adapter["recipe_api"] not in {1, 2}
    ):
        raise ProtocolError("invalid_request", "adapter descriptor values are invalid")
    operation = value["operation"]
    if not isinstance(operation, dict) or set(operation) != {"name", "context", "arguments"}:
        raise ProtocolError("invalid_request", "operation descriptor is invalid")
    if (
        not isinstance(operation["name"], str)
        or _OPERATION_RE.fullmatch(operation["name"]) is None
        or not isinstance(operation["context"], dict)
        or not isinstance(operation["arguments"], dict)
    ):
        raise ProtocolError("invalid_request", "operation descriptor values are invalid")
    if adapter["recipe_api"] == 1:
        if operation["name"] not in {"validate", "render", "stream", "reader"}:
            raise ProtocolError("invalid_request", "unknown recipe API 1 operation")
        expected_context = (
            {"capture_directory", "output_directory"}
            if operation["name"] in {"stream", "reader"}
            else {"capture_directory"}
        )
        if set(operation["context"]) != expected_context or not all(
            isinstance(operation["context"][key], str)
            and Path(operation["context"][key]).is_absolute()
            for key in expected_context
        ):
            raise ProtocolError("invalid_request", "recipe API 1 context is invalid")
    limits = value["limits"]
    limit_keys = {
        "memory_bytes",
        "file_size_bytes",
        "max_adapter_bytes",
        "max_protocol_bytes",
        "max_diagnostic_bytes",
    }
    if (
        not isinstance(limits, dict)
        or set(limits) != limit_keys
        or any(type(limits[key]) is not int or limits[key] <= 0 for key in limit_keys)
    ):
        raise ProtocolError("invalid_request", "worker limits are invalid")
    policy = value["policy"]
    if (
        not isinstance(policy, dict)
        or set(policy)
        != {
            "allow_python_network",
            "allow_python_subprocess",
            "filesystem_write_scope",
            "filesystem_write_root",
        }
        or type(policy["allow_python_network"]) is not bool
        or type(policy["allow_python_subprocess"]) is not bool
        or policy["filesystem_write_scope"] not in {"none", "reader_output"}
        or (
            policy["filesystem_write_scope"] == "none"
            and policy["filesystem_write_root"] is not None
        )
        or (
            policy["filesystem_write_scope"] == "reader_output"
            and (
                not isinstance(policy["filesystem_write_root"], str)
                or not Path(policy["filesystem_write_root"]).is_absolute()
            )
        )
    ):
        raise ProtocolError("invalid_request", "runtime policy is invalid")
    paths = value["diagnostic_paths"]
    if (
        not isinstance(paths, dict)
        or set(paths) != {"stdout", "stderr"}
        or any(not isinstance(item, str) or not Path(item).is_absolute() for item in paths.values())
    ):
        raise ProtocolError("invalid_request", "diagnostic paths are invalid")


def _redirect_diagnostics(paths: dict[str, str]) -> None:
    stdout_fd = os.open(paths["stdout"], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        stderr_fd = os.open(paths["stderr"], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except Exception:
        os.close(stdout_fd)
        raise
    os.dup2(stdout_fd, sys.stdout.fileno())
    os.dup2(stderr_fd, sys.stderr.fileno())
    os.close(stdout_fd)
    os.close(stderr_fd)


def _apply_controls(limits: dict[str, int], policy: dict[str, Any]) -> dict[str, Any]:
    controls: dict[str, Any] = {
        "reported": True,
        "memory_limit": _apply_rlimit("RLIMIT_AS", limits["memory_bytes"]),
        "file_size_limit": _apply_rlimit("RLIMIT_FSIZE", limits["file_size_bytes"]),
        "network": {
            "allowed": policy["allow_python_network"],
            "scope": "language-runtime policy",
            "os_enforced": False,
        },
        "subprocess": {
            "allowed": policy["allow_python_subprocess"],
            "scope": "language-runtime policy",
            "os_enforced": False,
        },
        "filesystem": {
            "write_scope": policy["filesystem_write_scope"],
            "enforced": False,
            "best_effort": True,
            "scope": "language-runtime policy",
            "os_enforced": False,
            "reason": "common Python/POSIX mutation APIs are wrapped; no OS filesystem sandbox is installed",
        },
        "full_os_sandbox": False,
    }
    if not policy["allow_python_network"]:
        _deny_python_network()
    if not policy["allow_python_subprocess"]:
        _deny_python_subprocess()
    _deny_python_filesystem_writes(policy["filesystem_write_root"])
    return controls


def _apply_rlimit(name: str, requested: int) -> dict[str, Any]:
    try:
        import resource
    except ImportError:
        return {"requested_bytes": requested, "enforced": False, "reason": "resource module unavailable"}
    resource_name = getattr(resource, name, None)
    if resource_name is None:
        return {"requested_bytes": requested, "enforced": False, "reason": f"{name} unavailable"}
    try:
        _soft, hard = resource.getrlimit(resource_name)
        infinity = resource.RLIM_INFINITY
        applied = requested if hard == infinity else min(requested, hard)
        resource.setrlimit(resource_name, (applied, applied))
        if name == "RLIMIT_FSIZE" and hasattr(signal, "SIGXFSZ"):
            signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    except (OSError, ValueError) as exc:
        return {"requested_bytes": requested, "enforced": False, "reason": str(exc)[:512]}
    return {
        "requested_bytes": requested,
        "enforced": True,
        "mechanism": name,
        "applied_bytes": applied,
    }


def _deny(*_args: Any, **_kwargs: Any) -> Any:
    raise PermissionError("denied by text-preserver Python-runtime adapter policy")


def _deny_python_network() -> None:
    import _socket
    import socket

    for name in (
        "socket",
        "SocketType",
        "socketpair",
        "fromfd",
        "create_connection",
        "create_server",
    ):
        if hasattr(socket, name):
            setattr(socket, name, _deny)
    if hasattr(_socket, "socket"):
        setattr(_socket, "socket", _deny)


def _deny_python_subprocess() -> None:
    import subprocess

    for name in (
        "Popen",
        "run",
        "call",
        "check_call",
        "check_output",
        "getoutput",
        "getstatusoutput",
    ):
        if hasattr(subprocess, name):
            setattr(subprocess, name, _deny)
    for name in (
        "execl",
        "execle",
        "execlp",
        "execlpe",
        "execv",
        "execve",
        "execvp",
        "execvpe",
        "system",
        "popen",
        "fork",
        "forkpty",
        "posix_spawn",
        "posix_spawnp",
        "spawnl",
        "spawnle",
        "spawnlp",
        "spawnlpe",
        "spawnv",
        "spawnve",
        "spawnvp",
        "spawnvpe",
    ):
        if hasattr(os, name):
            setattr(os, name, _deny)
    if os.name == "posix":
        import posix

        for name in (
            "execv",
            "execve",
            "fork",
            "forkpty",
            "posix_spawn",
            "posix_spawnp",
            "system",
        ):
            if hasattr(posix, name):
                setattr(posix, name, _deny)
    try:
        import _posixsubprocess
    except ImportError:
        pass
    else:
        if hasattr(_posixsubprocess, "fork_exec"):
            _posixsubprocess.fork_exec = _deny


def _deny_python_filesystem_writes(raw_root: str | None) -> None:
    """Wrap common Python mutation APIs; this is intentionally not an OS sandbox."""
    writable_root = Path(raw_root).resolve() if raw_root is not None else None

    def permitted(path: Any, *, dir_fd: int | None = None) -> bool:
        if writable_root is None or isinstance(path, int) or dir_fd is not None:
            return False
        try:
            candidate = Path(path)
            if not candidate.is_absolute():
                candidate = Path.cwd() / candidate
            return candidate.resolve(strict=False).is_relative_to(writable_root)
        except (OSError, TypeError, ValueError):
            return False

    def require(path: Any, *, dir_fd: int | None = None) -> None:
        if not permitted(path, dir_fd=dir_fd):
            raise PermissionError(
                "filesystem write denied by text-preserver language-runtime adapter policy"
            )

    original_builtin_open = builtins.open
    original_io_open = io.open
    original_os_open = os.open
    posix_module: Any | None = None
    original_posix_open: Any | None = None
    if os.name == "posix":
        import posix

        posix_module = posix
        original_posix_open = posix.open

    def guarded_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if any(character in mode for character in "wax+"):
            require(file)
        return original_builtin_open(file, mode, *args, **kwargs)

    def guarded_io_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if any(character in mode for character in "wax+"):
            require(file)
        return original_io_open(file, mode, *args, **kwargs)

    write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

    def guarded_os_open(
        path: Any,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if flags & write_flags:
            require(path, dir_fd=dir_fd)
        if dir_fd is None:
            return original_os_open(path, flags, mode)
        return original_os_open(path, flags, mode, dir_fd=dir_fd)

    builtins.open = guarded_open
    io.open = guarded_io_open
    os.open = guarded_os_open
    if posix_module is not None:
        assert original_posix_open is not None

        def guarded_posix_open(
            path: Any,
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            if flags & write_flags:
                require(path, dir_fd=dir_fd)
            if dir_fd is None:
                return original_posix_open(path, flags, mode)
            return original_posix_open(path, flags, mode, dir_fd=dir_fd)

        posix_module.open = guarded_posix_open

    def guard_one(function: Any) -> Any:
        def guarded(path: Any, *args: Any, **kwargs: Any) -> Any:
            require(path, dir_fd=kwargs.get("dir_fd"))
            return function(path, *args, **kwargs)

        return guarded

    def guard_two(function: Any) -> Any:
        def guarded(source: Any, destination: Any, *args: Any, **kwargs: Any) -> Any:
            require(source, dir_fd=kwargs.get("src_dir_fd"))
            require(destination, dir_fd=kwargs.get("dst_dir_fd"))
            return function(source, destination, *args, **kwargs)

        return guarded

    for name in (
        "chmod",
        "chown",
        "lchmod",
        "lchown",
        "mkdir",
        "mkfifo",
        "mknod",
        "remove",
        "rmdir",
        "truncate",
        "unlink",
        "utime",
    ):
        if hasattr(os, name):
            setattr(os, name, guard_one(getattr(os, name)))
    for name in ("fchmod", "fchown", "ftruncate", "fremovexattr", "fsetxattr"):
        if hasattr(os, name):
            setattr(os, name, _deny)
    for name in ("chflags", "lchflags", "removexattr", "setxattr"):
        if hasattr(os, name):
            setattr(os, name, guard_one(getattr(os, name)))
    for name in ("rename", "replace"):
        if hasattr(os, name):
            setattr(os, name, guard_two(getattr(os, name)))
    # Links can make writes through an allowed output path mutate external data.
    for name in ("link", "symlink"):
        if hasattr(os, name):
            setattr(os, name, _deny)
    if posix_module is not None:
        for name in (
            "chflags",
            "chmod",
            "chown",
            "lchflags",
            "lchown",
            "mkdir",
            "mkfifo",
            "mknod",
            "remove",
            "removexattr",
            "rmdir",
            "setxattr",
            "truncate",
            "unlink",
            "utime",
        ):
            if hasattr(posix_module, name):
                setattr(posix_module, name, guard_one(getattr(posix_module, name)))
        for name in ("rename", "replace"):
            if hasattr(posix_module, name):
                setattr(posix_module, name, guard_two(getattr(posix_module, name)))
        for name in (
            "fchmod",
            "fchown",
            "ftruncate",
            "fremovexattr",
            "fsetxattr",
            "link",
            "symlink",
        ):
            if hasattr(posix_module, name):
                setattr(posix_module, name, _deny)


def _invoke(request: dict[str, Any]) -> Any:
    adapter = request["adapter"]
    path = Path(adapter["path"])
    source = _verified_source(path, adapter["sha256"], request["limits"]["max_adapter_bytes"])
    previous_directory = Path.cwd()
    try:
        os.chdir(path.parent)
        module = _load_adapter(path, source)
        operation = request["operation"]
        name = operation["name"]
        context = operation["context"]
        arguments = operation["arguments"]
        if adapter["recipe_api"] == 1:
            entry_points = {
                "validate": "analyze_capture",
                "render": "render_static_reader",
                "stream": "write_static_reader",
            }
            if name == "reader":
                write = getattr(module, "write_static_reader", None)
                if callable(write):
                    return write(
                        Path(context["capture_directory"]),
                        output_directory=Path(context["output_directory"]),
                        **arguments,
                    )
                render = getattr(module, "render_static_reader", None)
                if callable(render):
                    return render(Path(context["capture_directory"]), **arguments)
                raise ProtocolError(
                    "missing_capability",
                    "recipe API 1 adapter does not export write_static_reader() or render_static_reader()",
                )
            function = getattr(module, entry_points[name], None)
            if not callable(function):
                raise ProtocolError("missing_capability", f"adapter does not export {entry_points[name]}()")
            capture_directory = Path(context["capture_directory"])
            if name == "stream":
                return function(
                    capture_directory,
                    output_directory=Path(context["output_directory"]),
                    **arguments,
                )
            return function(capture_directory, **arguments)
        if name in {"validate", "reader", "render", "stream"}:
            return _invoke_typed_api2(module, name, context, arguments)
        function = getattr(module, "handle_operation", None)
        if not callable(function):
            raise ProtocolError(
                "missing_capability",
                f"recipe API 2 adapter does not handle custom operation {name!r}",
            )
        return function(name, context=context, arguments=arguments)
    finally:
        os.chdir(previous_directory)


def _invoke_typed_api2(
    module: ModuleType,
    operation: str,
    context: dict[str, Any],
    arguments: dict[str, Any],
) -> dict[str, Any]:
    from text_preserver.adapters import (
        ReaderContext,
        ReaderReport,
        ValidationContext,
        ValidationReport,
    )

    if arguments:
        raise ProtocolError(
            "invalid_operation",
            "typed recipe API 2 operations take all inputs in context",
        )
    if operation == "validate":
        expected = {
            "capture_directory",
            "expected_work_count",
            "required_representation_kinds",
            "required_source_ids",
        }
        if set(context) != expected:
            raise ProtocolError("invalid_operation", "typed API 2 validation context is invalid")
        typed_context = ValidationContext(
            Path(context["capture_directory"]),
            context["expected_work_count"],
            tuple(context["required_representation_kinds"]),
            tuple(context["required_source_ids"]),
        )
        function = getattr(module, "validate", None)
        if not callable(function):
            raise ProtocolError(
                "missing_capability",
                "recipe API 2 adapter does not export validate()",
            )
        value = function(typed_context)
        if not isinstance(value, ValidationReport):
            raise ProtocolError(
                "invalid_result",
                f"recipe API 2 validate() returned {type(value).__name__}, expected ValidationReport",
            )
        if (
            value.status not in {"complete", "complete_with_warnings", "incomplete"}
            or type(value.errors) is not tuple
            or not all(isinstance(item, str) for item in value.errors)
            or type(value.warnings) is not tuple
            or not all(isinstance(item, str) for item in value.warnings)
            or not isinstance(value.details, Mapping)
        ):
            raise ProtocolError("invalid_result", "recipe API 2 validate() returned an invalid ValidationReport")
        details = dict(value.details)
        reserved = {"status", "errors", "warnings"} & set(details)
        if reserved:
            raise ProtocolError(
                "invalid_result",
                f"recipe API 2 validate() details contain reserved field {sorted(reserved)[0]!r}",
            )
        return {
            **details,
            "status": value.status,
            "errors": list(value.errors),
            "warnings": list(value.warnings),
        }
    if operation in {"reader", "render", "stream"}:
        expected = {"capture_directory", "output_directory", "expected_work_count"}
        if set(context) != expected:
            raise ProtocolError("invalid_operation", "typed API 2 reader context is invalid")
        reader_context = ReaderContext(
            Path(context["capture_directory"]),
            Path(context["output_directory"]),
            context["expected_work_count"],
        )
        function = getattr(module, "build_reader", None)
        if not callable(function):
            raise ProtocolError(
                "missing_capability",
                "recipe API 2 adapter does not export build_reader()",
            )
        value = function(reader_context)
        if not isinstance(value, ReaderReport):
            raise ProtocolError(
                "invalid_result",
                f"recipe API 2 build_reader() returned {type(value).__name__}, expected ReaderReport",
            )
        if (
            value.status not in {"complete", "complete_with_warnings", "incomplete"}
            or not isinstance(value.summary, Mapping)
            or type(value.warnings) is not tuple
            or not all(isinstance(item, str) for item in value.warnings)
            or (
                value.files is not None
                and (
                    not isinstance(value.files, Mapping)
                    or not all(
                        isinstance(name, str) and isinstance(content, str)
                        for name, content in value.files.items()
                    )
                )
            )
        ):
            raise ProtocolError("invalid_result", "recipe API 2 build_reader() returned an invalid ReaderReport")
        return {
            "status": value.status,
            "summary": dict(value.summary),
            "warnings": list(value.warnings),
            "files": dict(value.files) if value.files is not None else None,
        }
    raise ProtocolError(
        "missing_capability",
        "recipe API 2 adapter does not export handle_operation() for this operation",
    )


def _verified_source(path: Path, expected: str, max_bytes: int) -> bytes:
    if path.is_symlink():
        raise ProtocolError("adapter_verification", "adapter must not be a symlink")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ProtocolError("adapter_verification", "adapter is not a regular file")
        if info.st_size > max_bytes:
            raise ProtocolError("adapter_verification", f"adapter exceeds {max_bytes} bytes")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            block = os.read(descriptor, min(1024 * 1024, remaining))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
    except OSError as exc:
        raise ProtocolError("adapter_verification", f"cannot open adapter: {exc}") from exc
    finally:
        if "descriptor" in locals():
            os.close(descriptor)
    source = b"".join(chunks)
    if len(source) > max_bytes or hashlib.sha256(source).hexdigest() != expected:
        raise ProtocolError("adapter_verification", "adapter bytes do not match expected SHA-256")
    return source


def _load_adapter(path: Path, source: bytes) -> ModuleType:
    directory_identity = hashlib.sha256(str(path.parent).encode()).hexdigest()[:16]
    package_name = f"text_preserver_recipe_{directory_identity}"
    module_name = f"{package_name}.adapter"
    importlib.invalidate_caches()
    package = ModuleType(package_name)
    package.__file__ = str(path.parent)
    package.__package__ = package_name
    package.__path__ = [str(path.parent)]
    package.__spec__ = ModuleSpec(package_name, loader=None, is_package=True)
    package.__spec__.submodule_search_locations = [str(path.parent)]
    sys.modules[package_name] = package
    module = ModuleType(module_name)
    module.__file__ = str(path)
    module.__package__ = package_name
    module.__spec__ = ModuleSpec(module_name, loader=None, origin=str(path))
    sys.modules[module_name] = module
    previous_bytecode = sys.dont_write_bytecode
    previous_directory = Path.cwd()
    sys.dont_write_bytecode = True
    try:
        os.chdir(path.parent)
        exec(compile(source, str(path), "exec"), module.__dict__)
    finally:
        os.chdir(previous_directory)
        sys.dont_write_bytecode = previous_bytecode
    return module


def _response(
    request_id: str,
    ok: bool,
    result: Any,
    error: dict[str, str] | None,
    controls: dict[str, Any],
    request: dict[str, Any] | None,
    *,
    include_diagnostics: bool = True,
) -> dict[str, Any]:
    diagnostics = _diagnostics(request) if include_diagnostics else {
        "stdout": "",
        "stderr": "",
        "stdout_truncated": True,
        "stderr_truncated": True,
    }
    return {
        "protocol": PROTOCOL_NAME,
        "version": PROTOCOL_VERSION,
        "kind": "response",
        "request_id": request_id,
        "ok": ok,
        "result": result,
        "error": error,
        "diagnostics": diagnostics,
        "controls": controls,
    }


def _diagnostics(request: dict[str, Any] | None) -> dict[str, Any]:
    if request is None or "diagnostic_paths" not in request or "limits" not in request:
        return {"stdout": "", "stderr": "", "stdout_truncated": False, "stderr_truncated": False}
    limit = request["limits"]["max_diagnostic_bytes"]
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError):
            pass
    result: dict[str, Any] = {}
    for name in ("stdout", "stderr"):
        try:
            with Path(request["diagnostic_paths"][name]).open("rb") as stream:
                value = stream.read(limit + 1)
        except OSError:
            value = b""
        result[name] = value[:limit].decode("utf-8", "replace")
        result[f"{name}_truncated"] = len(value) > limit
    return result


def _unavailable_controls() -> dict[str, Any]:
    return {
        "reported": False,
        "reason": "request was rejected before worker controls were applied",
        "full_os_sandbox": False,
    }


def _encode(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


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

    return json.loads(
        payload,
        parse_constant=reject_constant,
        object_pairs_hook=reject_duplicate_keys,
    )


if __name__ == "__main__":
    raise SystemExit(main())
