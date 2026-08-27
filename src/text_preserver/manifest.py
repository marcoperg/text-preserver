"""Create and verify immutable capture fixity manifests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any


MANIFEST_NAME = "manifest-sha256.json"
SUMS_NAME = "SHA256SUMS"
DIGEST_RE = re.compile(r"^[a-f0-9]{64}$")
EMPTY_DIGEST = hashlib.sha256(b"").hexdigest()


class ManifestError(RuntimeError):
    """Raised when a capture cannot be finalized safely."""


@dataclass(frozen=True)
class VerificationResult:
    capture_directory: Path
    checked_objects: int
    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "capture_directory": str(self.capture_directory),
            "ok": self.ok,
            "checked_objects": self.checked_objects,
            "errors": list(self.errors),
        }


def finalize_capture(capture_directory: Path) -> dict[str, Any]:
    """Write conventional and structured SHA-256 manifests exactly once."""
    if capture_directory.is_symlink():
        raise ManifestError(f"capture directory must not be a symlink: {capture_directory}")
    root = capture_directory.resolve()
    manifest_path = root / MANIFEST_NAME
    sums_path = root / SUMS_NAME
    if manifest_path.exists() or manifest_path.is_symlink():
        raise ManifestError(f"capture is already finalized: {manifest_path}")
    entries = _scan_objects(root, excluded={MANIFEST_NAME, SUMS_NAME})
    sums = "".join(
        f"{entry['sha256']}  {entry['path']}\n"
        for entry in entries
        if entry["type"] == "file"
    ).encode("utf-8")
    _atomic_write_bytes(sums_path, sums, replace_existing=True)
    entries.append(
        {
            "path": SUMS_NAME,
            "type": "file",
            "size": len(sums),
            "sha256": hashlib.sha256(sums).hexdigest(),
        }
    )
    entries.sort(key=lambda entry: entry["path"])
    manifest = {
        "schema_version": 1,
        "algorithm": "sha256",
        "created_at": _utc_now(),
        "files": entries,
    }
    _atomic_write_bytes(
        manifest_path,
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        replace_existing=False,
    )
    return manifest


def verify_capture(path: str | Path) -> VerificationResult:
    """Verify expected objects and reject additions inside a finalized capture."""
    try:
        root = _resolve_capture_directory(Path(path))
    except ManifestError as exc:
        return VerificationResult(Path(path).expanduser(), 0, (str(exc),))
    manifest_path = root / MANIFEST_NAME
    try:
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise ManifestError("manifest must be a regular file")
        manifest = json.loads(_read_regular(manifest_path).decode("utf-8"))
        expected = _expected_entries(manifest)
    except (OSError, UnicodeError, json.JSONDecodeError, ManifestError) as exc:
        return VerificationResult(root, 0, (f"invalid fixity manifest: {exc}",))

    errors: list[str] = []
    try:
        actual_entries = _scan_objects(root, excluded={MANIFEST_NAME})
    except (OSError, ManifestError) as exc:
        return VerificationResult(root, 0, (f"cannot inventory capture: {exc}",))
    actual = {entry["path"]: entry for entry in actual_entries}

    for relative in sorted(set(expected) - set(actual)):
        errors.append(f"missing object: {relative}")
    for relative in sorted(set(actual) - set(expected)):
        errors.append(f"unexpected object: {relative}")
    checked = 0
    for relative in sorted(set(expected) & set(actual)):
        checked += 1
        wanted = expected[relative]
        found = actual[relative]
        for field in ("type", "size", "sha256"):
            if found.get(field) != wanted.get(field):
                errors.append(
                    f"{relative}: {field} mismatch "
                    f"(expected {wanted.get(field)!r}, found {found.get(field)!r})"
                )
    return VerificationResult(root, checked, tuple(errors))


def _resolve_capture_directory(path: Path) -> Path:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise ManifestError(f"capture directory must not be a symlink: {expanded}")
    candidate = expanded.resolve()
    if not candidate.is_dir():
        raise ManifestError(f"capture directory does not exist: {candidate}")
    manifest = candidate / MANIFEST_NAME
    if manifest.is_symlink():
        raise ManifestError(f"manifest must not be a symlink: {manifest}")
    if manifest.is_file():
        return candidate
    latest = candidate / "LATEST"
    if latest.is_symlink():
        raise ManifestError(f"LATEST must not be a symlink: {latest}")
    if latest.is_file():
        try:
            value = _read_regular(latest).decode("utf-8").strip()
        except (OSError, UnicodeError) as exc:
            raise ManifestError(f"cannot read LATEST pointer: {exc}") from exc
        if not value or Path(value).is_absolute() or ".." in Path(value).parts:
            raise ManifestError(f"unsafe LATEST pointer: {latest}")
        target = (candidate / value).resolve()
        if not target.is_relative_to(candidate):
            raise ManifestError(f"LATEST escapes collection directory: {latest}")
        if (target / MANIFEST_NAME).is_file():
            return target
    raise ManifestError(f"fixity manifest not found under: {candidate}")


def _expected_entries(manifest: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(manifest, dict):
        raise ManifestError("root must be an object")
    if type(manifest.get("schema_version")) is not int or manifest.get("schema_version") != 1:
        raise ManifestError("unsupported schema version")
    if manifest.get("algorithm") != "sha256":
        raise ManifestError("unsupported schema version or algorithm")
    values = manifest.get("files")
    if not isinstance(values, list):
        raise ManifestError("files must be an array")
    entries: dict[str, dict[str, Any]] = {}
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            raise ManifestError(f"files[{index}] must be an object")
        relative = value.get("path")
        if not isinstance(relative, str) or not _safe_relative_path(relative):
            raise ManifestError(f"files[{index}] has an unsafe path")
        if relative in entries:
            raise ManifestError(f"duplicate manifest path: {relative}")
        if value.get("type") not in {"file", "directory"}:
            raise ManifestError(f"files[{index}] has an unsupported type")
        if type(value.get("size")) is not int or value["size"] < 0:
            raise ManifestError(f"files[{index}] has an invalid size")
        digest = value.get("sha256")
        if not isinstance(digest, str) or DIGEST_RE.fullmatch(digest) is None:
            raise ManifestError(f"files[{index}] has an invalid SHA-256 digest")
        entries[relative] = value
    return entries


def _scan_objects(root: Path, *, excluded: set[str]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    def traversal_error(error: OSError) -> None:
        raise error

    for directory, directory_names, file_names in os.walk(
        root,
        followlinks=False,
        onerror=traversal_error,
    ):
        base = Path(directory)
        for name in directory_names:
            path = base / name
            if path.is_symlink():
                raise ManifestError(f"symlinks are not allowed in captures: {path}")
            entries.append(_object_entry(root, path))
        for name in file_names:
            path = base / name
            relative = path.relative_to(root).as_posix()
            if relative in excluded:
                continue
            entries.append(_object_entry(root, path))
    return sorted(entries, key=lambda entry: entry["path"])


def _object_entry(root: Path, path: Path) -> dict[str, Any]:
    relative = path.relative_to(root).as_posix()
    if not _safe_relative_path(relative) or "\n" in relative or "\\" in relative:
        raise ManifestError(f"path cannot be represented safely: {relative!r}")
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise ManifestError(f"symlinks are not allowed in captures: {relative}")
    if stat.S_ISDIR(info.st_mode):
        return {
            "path": relative,
            "type": "directory",
            "size": 0,
            "sha256": EMPTY_DIGEST,
        }
    if not stat.S_ISREG(info.st_mode):
        raise ManifestError(f"unsupported object type: {relative}")
    return {
        "path": relative,
        "type": "file",
        "size": info.st_size,
        "sha256": _hash_file(path, info),
    }


def _hash_file(path: Path, expected: os.stat_result) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or (before.st_dev, before.st_ino) != (
            expected.st_dev,
            expected.st_ino,
        ):
            raise ManifestError(f"object changed while hashing: {path}")
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
        after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ManifestError(f"object changed while hashing: {path}")
    return digest.hexdigest()


def _read_regular(path: Path) -> bytes:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ManifestError(f"expected a regular file: {path}")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise ManifestError(f"file changed while opening: {path}")
        return stream.read()


def _safe_relative_path(value: str) -> bool:
    path = Path(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts


def _atomic_write_bytes(path: Path, value: bytes, *, replace_existing: bool) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        if replace_existing:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
            temporary.unlink()
    finally:
        if temporary.exists():
            temporary.unlink()
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
