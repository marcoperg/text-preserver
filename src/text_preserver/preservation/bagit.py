"""Atomic BagIt 1.0 exports and dependency-free independent validation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import sys
from typing import Any, Mapping

from text_preserver import __version__
from text_preserver.preservation.payload_roles import (
    ExportPolicy,
    ExportProfile,
    PayloadRoleError,
    VerifiedCapture,
    assert_capture_unchanged,
)


BAGIT_DECLARATION = b"BagIt-Version: 1.0\nTag-File-Character-Encoding: UTF-8\n"
_MANIFEST_RE = re.compile(r"^manifest-([a-z0-9]+)\.txt$")
_TAGMANIFEST_RE = re.compile(r"^tagmanifest-([a-z0-9]+)\.txt$")
_DIGEST_LENGTHS = {"md5": 32, "sha1": 40, "sha256": 64, "sha512": 128}


class BagItError(RuntimeError):
    """Raised when a BagIt export cannot be created safely."""


@dataclass(frozen=True)
class BagCreationResult:
    path: Path
    profile: ExportProfile
    policy_identifier: str
    payload_files: int
    payload_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "profile": self.profile.value,
            "policy_identifier": self.policy_identifier,
            "payload_files": self.payload_files,
            "payload_bytes": self.payload_bytes,
        }


@dataclass(frozen=True)
class BagValidationResult:
    path: Path
    checked_files: int
    errors: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "ok": self.ok,
            "checked_files": self.checked_files,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def create_bag(
    capture: VerifiedCapture,
    destination: str | Path,
    *,
    profile: ExportProfile | str,
    policy: ExportPolicy,
    bag_info: Mapping[str, str] | None = None,
) -> BagCreationResult:
    """Atomically create a private complete-capture or allowlisted public bag."""
    resolved_profile = _profile(profile)
    if capture.metadata.get("status") not in {"complete", "complete_with_warnings"}:
        raise BagItError("BagIt export requires a complete verified capture")
    try:
        assert_capture_unchanged(capture)
        selected = policy.selected_paths(resolved_profile, capture.files)
    except PayloadRoleError as exc:
        raise BagItError(str(exc)) from exc
    if not selected:
        raise BagItError("BagIt export policy selected no payload files")

    requested_target = Path(destination).expanduser()
    if requested_target.exists() or requested_target.is_symlink():
        raise BagItError(f"BagIt destination already exists: {requested_target}")
    target = requested_target.parent.resolve() / requested_target.name
    if not target.parent.is_dir():
        raise BagItError(f"BagIt destination parent does not exist: {target.parent}")

    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
    try:
        data_root = temporary / "data" / "capture"
        data_root.mkdir(parents=True)
        if resolved_profile is ExportProfile.PRIVATE:
            for relative in capture.directories:
                (data_root / PurePosixPath(relative)).mkdir(parents=True, exist_ok=True)
        assignment_by_path = {item.path: item for item in capture.assignments}
        mappings: list[dict[str, Any]] = []
        payload_bytes = 0
        for relative in sorted(selected):
            source = capture.directory / PurePosixPath(relative)
            bag_relative = f"data/capture/{relative}"
            destination_path = temporary / PurePosixPath(bag_relative)
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            _copy_regular(source, destination_path)
            size = destination_path.stat().st_size
            digest = _hash_file(destination_path, "sha256")
            if digest != capture.file_sha256[relative]:
                raise BagItError(f"capture file changed while exporting: {relative}")
            payload_bytes += size
            mappings.append(
                {
                    "bag_path": bag_relative,
                    "capture_path": relative,
                    "role": assignment_by_path[relative].role.value,
                    "bytes": size,
                    "sha256": digest,
                }
            )

        _write(temporary / "bagit.txt", BAGIT_DECLARATION)
        for algorithm in ("sha256", "sha512"):
            lines = [
                f"{_hash_file(temporary / PurePosixPath(item['bag_path']), algorithm)}  "
                f"{_encode_manifest_path(item['bag_path'])}\n"
                for item in mappings
            ]
            _write(temporary / f"manifest-{algorithm}.txt", "".join(lines).encode("utf-8"))

        info: dict[str, str] = dict(bag_info or {})
        info.update({
            "Bagging-Date": date.today().isoformat(),
            "External-Identifier": str(capture.metadata.get("capture_id", capture.directory.name)),
            "Payload-Oxum": f"{payload_bytes}.{len(mappings)}",
            "Text-Preserver-Export-Profile": resolved_profile.value,
            "Text-Preserver-Policy": policy.identifier,
        })
        _write(temporary / "bag-info.txt", _bag_info_bytes(info))
        export_metadata = {
            "schema_version": 1,
            "bagit_version": "1.0",
            "capture_manifest_sha256": capture.manifest_sha256,
            "export_profile": resolved_profile.value,
            "export_policy": policy.identifier,
            "export_tool": f"text-preserver {__version__}",
            "capture": {
                key: capture.metadata[key]
                for key in (
                    "capture_id",
                    "collection_id",
                    "status",
                    "started_at",
                    "ended_at",
                )
                if key in capture.metadata
            },
            "files": mappings,
        }
        _write(
            temporary / "text-preserver-export.json",
            (json.dumps(export_metadata, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )

        tag_paths = (
            "bag-info.txt",
            "bagit.txt",
            "manifest-sha256.txt",
            "manifest-sha512.txt",
            "text-preserver-export.json",
        )
        for algorithm in ("sha256", "sha512"):
            lines = [
                f"{_hash_file(temporary / path, algorithm)}  {path}\n" for path in tag_paths
            ]
            _write(
                temporary / f"tagmanifest-{algorithm}.txt",
                "".join(lines).encode("utf-8"),
            )

        validation = validate_bag(temporary)
        if not validation.ok:
            raise BagItError("created bag failed validation: " + "; ".join(validation.errors))
        try:
            assert_capture_unchanged(capture)
        except PayloadRoleError as exc:
            raise BagItError(str(exc)) from exc
        _fsync_tree(temporary)
        _rename_no_replace(temporary, target)
        _fsync_directory(target.parent)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return BagCreationResult(
        path=target,
        profile=resolved_profile,
        policy_identifier=policy.identifier,
        payload_files=len(selected),
        payload_bytes=payload_bytes,
    )


def validate_bag(path: str | Path) -> BagValidationResult:
    """Validate BagIt structure and checksums without using creator metadata."""
    requested_root = Path(path).expanduser()
    if requested_root.is_symlink():
        return BagValidationResult(requested_root, 0, ("bag must be a non-symlink directory",))
    root = requested_root.resolve()
    errors: list[str] = []
    warnings: list[str] = []
    checked = 0
    if not root.is_dir() or root.is_symlink():
        return BagValidationResult(root, 0, ("bag must be a non-symlink directory",))
    try:
        actual_files = _inventory(root)
    except (OSError, BagItError) as exc:
        return BagValidationResult(root, 0, (f"cannot inventory bag: {exc}",))
    data_files = frozenset(path for path in actual_files if path.startswith("data/"))
    if not (root / "data").is_dir() or (root / "data").is_symlink():
        errors.append("required payload directory data/ is missing or unsafe")
    if not data_files:
        errors.append("bag contains no payload files")
    try:
        declaration = _read_regular(root / "bagit.txt")
        if not _valid_declaration(declaration):
            errors.append("bagit.txt is not a valid BagIt 1.0 UTF-8 declaration")
    except (OSError, BagItError) as exc:
        errors.append(f"cannot read bagit.txt: {exc}")

    payload_manifests = sorted(
        path for path in actual_files if _MANIFEST_RE.fullmatch(path) is not None
    )
    if not payload_manifests:
        errors.append("bag has no payload manifest")
    for manifest_path in payload_manifests:
        match = _MANIFEST_RE.fullmatch(manifest_path)
        assert match is not None
        algorithm = match.group(1)
        try:
            entries = _parse_manifest(root / manifest_path, algorithm, payload=True)
        except (OSError, UnicodeError, BagItError) as exc:
            errors.append(f"invalid {manifest_path}: {exc}")
            continue
        listed = frozenset(entries)
        for missing in sorted(data_files - listed):
            errors.append(f"{manifest_path} does not list payload file: {missing}")
        for unexpected in sorted(listed - data_files):
            errors.append(f"{manifest_path} references missing payload file: {unexpected}")
        for relative in sorted(listed & data_files):
            checked += 1
            found = _hash_file(root / PurePosixPath(relative), algorithm)
            if found.lower() != entries[relative].lower():
                errors.append(f"{relative}: {algorithm} mismatch")

    tag_manifests = sorted(
        path for path in actual_files if _TAGMANIFEST_RE.fullmatch(path) is not None
    )
    tag_sets: list[frozenset[str]] = []
    for manifest_path in tag_manifests:
        match = _TAGMANIFEST_RE.fullmatch(manifest_path)
        assert match is not None
        algorithm = match.group(1)
        try:
            entries = _parse_manifest(root / manifest_path, algorithm, payload=False)
        except (OSError, UnicodeError, BagItError) as exc:
            errors.append(f"invalid {manifest_path}: {exc}")
            continue
        listed = frozenset(entries)
        tag_sets.append(listed)
        for required in payload_manifests:
            if required not in listed:
                errors.append(f"{manifest_path} does not list payload manifest: {required}")
        for relative, wanted in sorted(entries.items()):
            if relative not in actual_files:
                errors.append(f"{manifest_path} references missing tag file: {relative}")
                continue
            checked += 1
            found = _hash_file(root / PurePosixPath(relative), algorithm)
            if found.lower() != wanted.lower():
                errors.append(f"{relative}: {algorithm} tag checksum mismatch")
        unlisted = sorted(
            path
            for path in actual_files
            if not path.startswith("data/")
            and _TAGMANIFEST_RE.fullmatch(path) is None
            and path not in listed
        )
        if unlisted:
            warnings.append(f"{manifest_path} does not list tag file: {unlisted[0]}")
    if tag_sets and any(value != tag_sets[0] for value in tag_sets[1:]):
        errors.append("tag manifests do not list the same set of tag files")

    _validate_oxum(root, data_files, errors)
    return BagValidationResult(root, checked, tuple(errors), tuple(warnings))


def _parse_manifest(path: Path, algorithm: str, *, payload: bool) -> dict[str, str]:
    if algorithm not in _DIGEST_LENGTHS:
        raise BagItError(f"unsupported checksum algorithm: {algorithm}")
    raw = _read_regular(path)
    if raw.startswith(b"\xef\xbb\xbf"):
        raise BagItError("UTF-8 tag file must not contain a BOM")
    text = raw.decode("utf-8")
    if text and not text.endswith(("\n", "\r")):
        raise BagItError("last manifest line is not terminated")
    entries: dict[str, str] = {}
    for line_number, line in enumerate(text.splitlines(), 1):
        match = re.fullmatch(r"([0-9A-Fa-f]+)[ \t]+(.+)", line)
        if match is None or len(match.group(1)) != _DIGEST_LENGTHS[algorithm]:
            raise BagItError(f"malformed line {line_number}")
        relative = _decode_manifest_path(match.group(2))
        if payload != relative.startswith("data/"):
            kind = "payload" if payload else "tag"
            raise BagItError(f"{kind} manifest has path in the wrong area: {relative}")
        if _TAGMANIFEST_RE.fullmatch(relative) is not None:
            raise BagItError("tag manifest must not list a tag manifest")
        if relative in entries:
            raise BagItError(f"duplicate manifest path: {relative}")
        entries[relative] = match.group(1)
    if not entries:
        raise BagItError("manifest is empty")
    return entries


def _inventory(root: Path) -> frozenset[str]:
    files: set[str] = set()
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in directory_names:
            info = (base / name).lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise BagItError(f"unsafe bag directory object: {base / name}")
        for name in file_names:
            path = base / name
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise BagItError(f"unsafe bag file object: {path}")
            relative = path.relative_to(root).as_posix()
            _safe_bag_path(relative)
            files.add(relative)
    return frozenset(files)


def _validate_oxum(root: Path, data_files: frozenset[str], errors: list[str]) -> None:
    path = root / "bag-info.txt"
    if not path.is_file() or path.is_symlink():
        return
    try:
        text = _read_regular(path).decode("utf-8")
    except (OSError, UnicodeError, BagItError) as exc:
        errors.append(f"invalid bag-info.txt: {exc}")
        return
    values = re.findall(r"(?im)^Payload-Oxum: ([0-9]+\.[0-9]+)$", text)
    if len(values) > 1:
        errors.append("bag-info.txt repeats Payload-Oxum")
    elif values:
        octets = sum((root / PurePosixPath(value)).stat().st_size for value in data_files)
        expected = f"{octets}.{len(data_files)}"
        if values[0] != expected:
            errors.append(f"Payload-Oxum mismatch (expected {expected}, found {values[0]})")


def _bag_info_bytes(values: Mapping[str, str]) -> bytes:
    lines: list[str] = []
    for label, value in values.items():
        if (
            not isinstance(label, str)
            or not label
            or label != label.strip()
            or ":" in label
            or any(character in label for character in "\r\n")
            or not isinstance(value, str)
            or not value
            or any(character in value for character in "\r\n")
        ):
            raise BagItError(f"invalid bag-info metadata element: {label!r}")
        lines.append(f"{label}: {value}\n")
    return "".join(lines).encode("utf-8")


def _valid_declaration(value: bytes) -> bool:
    if value.startswith(b"\xef\xbb\xbf") or not value.endswith((b"\r", b"\n")):
        return False
    try:
        lines = value.decode("utf-8").splitlines()
    except UnicodeError:
        return False
    return lines == [
        "BagIt-Version: 1.0",
        "Tag-File-Character-Encoding: UTF-8",
    ]


def _copy_regular(source: Path, destination: Path) -> None:
    info = source.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise BagItError(f"capture payload is not a regular file: {source}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(source, flags)
    with os.fdopen(descriptor, "rb") as input_stream, destination.open("xb") as output_stream:
        opened = os.fstat(input_stream.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise BagItError(f"capture file changed while opening: {source}")
        shutil.copyfileobj(input_stream, output_stream, 1024 * 1024)
        output_stream.flush()
        os.fsync(output_stream.fileno())
        after = os.fstat(input_stream.fileno())
        if (opened.st_size, opened.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise BagItError(f"capture file changed while copying: {source}")


def _write(path: Path, value: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def _read_regular(path: Path) -> bytes:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise BagItError(f"expected a regular file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise BagItError(f"file changed while opening: {path}")
        return stream.read()


def _hash_file(path: Path, algorithm: str) -> str:
    try:
        digest = hashlib.new(algorithm)
    except ValueError as exc:
        raise BagItError(f"unsupported checksum algorithm: {algorithm}") from exc
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _encode_manifest_path(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _decode_manifest_path(value: str) -> str:
    if re.search(r"%(?!0[dD]|0[aA]|25)", value):
        raise BagItError(f"invalid percent encoding in manifest path: {value!r}")
    value = re.sub("%0[dD]", "\r", value)
    value = re.sub("%0[aA]", "\n", value)
    value = value.replace("%25", "%")
    return _safe_bag_path(value)


def _safe_bag_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or value != path.as_posix()
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in value
    ):
        raise BagItError(f"unsafe bag path: {value!r}")
    return value


def _profile(value: ExportProfile | str) -> ExportProfile:
    try:
        return ExportProfile(value)
    except ValueError as exc:
        raise BagItError(f"unsupported export profile: {value!r}") from exc


def _fsync_tree(root: Path) -> None:
    directories = [root]
    directories.extend(path for path in root.rglob("*") if path.is_dir())
    for directory in reversed(directories):
        _fsync_directory(directory)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rename_no_replace(source: Path, target: Path) -> None:
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        renamex_np = getattr(libc, "renamex_np", None)
        if renamex_np is not None:
            result = renamex_np(os.fsencode(source), os.fsencode(target), 0x00000004)
            if result == 0:
                return
            error = ctypes.get_errno()
            if error == errno.EEXIST:
                raise BagItError(f"BagIt destination already exists: {target}")
            raise OSError(error, os.strerror(error), target)
    elif sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is not None:
            result = renameat2(-100, os.fsencode(source), -100, os.fsencode(target), 1)
            if result == 0:
                return
            error = ctypes.get_errno()
            if error == errno.EEXIST:
                raise BagItError(f"BagIt destination already exists: {target}")
            if error not in {errno.ENOSYS, errno.EINVAL}:
                raise OSError(error, os.strerror(error), target)
    if os.name == "nt":  # Windows rename is no-replace.
        try:
            os.rename(source, target)
        except FileExistsError as exc:
            raise BagItError(f"BagIt destination already exists: {target}") from exc
        return
    raise BagItError("atomic no-replace directory publication is unavailable on this platform")
