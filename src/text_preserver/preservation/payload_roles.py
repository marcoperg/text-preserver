"""Resolve capture files to preservation roles without changing captures."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
from types import MappingProxyType
from typing import Any, Mapping

from text_preserver.preservation.fixity import MANIFEST_NAME, verify_capture


class PayloadRoleError(ValueError):
    """Raised when capture payload roles are absent, ambiguous, or unsafe."""


class PayloadRole(str, Enum):
    PRESERVATION_ORIGINAL = "preservation_original"
    CAPTURE_DERIVATIVE = "capture_derivative"
    METADATA = "metadata"


class ExportProfile(str, Enum):
    PRIVATE = "private"
    PUBLIC = "public"


@dataclass(frozen=True)
class ExportPolicy:
    """An auditable export selection policy.

    Private policies must leave ``included_paths`` as ``None``. Public
    policies must explicitly enumerate every capture file permitted to leave
    the private archive.
    """

    identifier: str
    included_paths: frozenset[str] | None

    def selected_paths(
        self,
        profile: ExportProfile | str,
        available_paths: frozenset[str],
    ) -> frozenset[str]:
        resolved = _export_profile(profile)
        if not self.identifier or "\r" in self.identifier or "\n" in self.identifier:
            raise PayloadRoleError("export policy identifier must be a non-empty single line")
        if resolved is ExportProfile.PRIVATE:
            if self.included_paths is not None:
                raise PayloadRoleError("private export policy must include the complete capture")
            return available_paths
        if self.included_paths is None:
            raise PayloadRoleError("public export policy requires an explicit path allowlist")
        normalized = frozenset(_safe_capture_path(value) for value in self.included_paths)
        unknown = sorted(normalized - available_paths)
        if unknown:
            raise PayloadRoleError(f"export policy references unknown capture path: {unknown[0]}")
        return normalized


@dataclass(frozen=True, order=True)
class RoleAssignment:
    path: str
    role: PayloadRole


@dataclass(frozen=True)
class VerifiedCapture:
    directory: Path
    metadata: Mapping[str, Any]
    schema_version: int
    manifest_sha256: str
    assignments: tuple[RoleAssignment, ...]
    file_sha256: Mapping[str, str]
    directories: tuple[str, ...]

    @property
    def files(self) -> frozenset[str]:
        return frozenset(assignment.path for assignment in self.assignments)

    def role_for(self, path: str) -> PayloadRole:
        normalized = _safe_capture_path(path)
        for assignment in self.assignments:
            if assignment.path == normalized:
                return assignment.role
        raise KeyError(normalized)


def export_policy(
    capture: VerifiedCapture,
    profile: ExportProfile | str,
) -> ExportPolicy:
    """Return the built-in complete or public-provenance export policy."""
    resolved = _export_profile(profile)
    if resolved is ExportProfile.PRIVATE:
        return ExportPolicy("private-complete-v1", None)
    selected = {
        assignment.path
        for assignment in capture.assignments
        if (
            assignment.role
            in {PayloadRole.PRESERVATION_ORIGINAL, PayloadRole.CAPTURE_DERIVATIVE}
            and _shareable_payload_path(assignment.path)
        )
        or _shareable_metadata_path(assignment.path)
    }
    return ExportPolicy("public-provenance-v1", frozenset(selected))


def preservation_warc_paths(capture: VerifiedCapture) -> tuple[str, ...]:
    """Return capture-relative preservation-original WARC paths."""
    return tuple(
        assignment.path
        for assignment in capture.assignments
        if assignment.role is PayloadRole.PRESERVATION_ORIGINAL
        and _is_warc_container(assignment.path)
    )


def load_verified_capture(capture_directory: str | Path) -> VerifiedCapture:
    """Verify a finalized capture and resolve every regular file to one role.

    Capture schema 3 uses the strict ``payload_roles`` array in ``capture.json``.
    Each item must contain exactly ``path`` and ``role``, paths must be unique,
    and the array must cover every regular file in the finalized capture.
    Schemas 1 and 2 are inferred from fixed source paths and preserved source
    metadata. WARC containers and direct HTTP-file deposits are originals,
    web mirrors and WARC indexes are derivatives, and other files are metadata.
    """
    root = Path(capture_directory).expanduser()
    result = verify_capture(root)
    if not result.ok:
        raise PayloadRoleError("capture fixity verification failed: " + "; ".join(result.errors))
    root = result.capture_directory
    capture_path = root / "capture.json"
    try:
        metadata_value = json.loads(_read_regular(capture_path).decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PayloadRoleError(f"invalid capture metadata: {exc}") from exc
    if not isinstance(metadata_value, dict):
        raise PayloadRoleError("capture metadata must be an object")
    schema_version = metadata_value.get("schema_version", 1)
    if type(schema_version) is not int or schema_version not in {1, 2, 3}:
        raise PayloadRoleError(f"unsupported capture schema version: {schema_version!r}")

    files = _capture_files(root)
    if schema_version == 3:
        assignments = _explicit_roles(metadata_value, files)
    else:
        assignments = _inferred_roles(root, files)
    manifest_sha256 = _sha256_file(root / MANIFEST_NAME)
    file_sha256, directories = _fixity_objects(root, manifest_sha256)
    return VerifiedCapture(
        directory=root,
        metadata=MappingProxyType(metadata_value),
        schema_version=schema_version,
        manifest_sha256=manifest_sha256,
        assignments=assignments,
        file_sha256=MappingProxyType(file_sha256),
        directories=directories,
    )


def assert_capture_unchanged(capture: VerifiedCapture) -> None:
    """Re-verify a capture and ensure it is the snapshot used by an exporter."""
    result = verify_capture(capture.directory)
    if not result.ok:
        raise PayloadRoleError("capture changed during export: " + "; ".join(result.errors))
    if _sha256_file(capture.directory / MANIFEST_NAME) != capture.manifest_sha256:
        raise PayloadRoleError("capture fixity manifest changed during export")


def _explicit_roles(
    metadata: Mapping[str, Any],
    files: frozenset[str],
) -> tuple[RoleAssignment, ...]:
    values = metadata.get("payload_roles")
    if not isinstance(values, list):
        raise PayloadRoleError("capture schema 3 requires a payload_roles array")
    assignments: dict[str, PayloadRole] = {}
    for index, value in enumerate(values):
        if not isinstance(value, dict) or set(value) != {"path", "role"}:
            raise PayloadRoleError(
                f"payload_roles[{index}] must contain exactly path and role"
            )
        path_value = value.get("path")
        if not isinstance(path_value, str):
            raise PayloadRoleError(f"payload_roles[{index}].path must be a string")
        path = _safe_capture_path(path_value)
        if path in assignments:
            raise PayloadRoleError(f"duplicate payload role path: {path}")
        role_value = value.get("role")
        try:
            role = PayloadRole(role_value)
        except (TypeError, ValueError) as exc:
            raise PayloadRoleError(
                f"payload_roles[{index}] has unsupported role: {role_value!r}"
            ) from exc
        if role is not PayloadRole.METADATA and not _payload_path(path):
            raise PayloadRoleError(f"payload role is not permitted for metadata path: {path}")
        assignments[path] = role
    missing = sorted(files - assignments.keys())
    unexpected = sorted(assignments.keys() - files)
    if missing:
        raise PayloadRoleError(f"schema 3 has no explicit role for capture file: {missing[0]}")
    if unexpected:
        raise PayloadRoleError(f"schema 3 role references a non-file path: {unexpected[0]}")
    return tuple(RoleAssignment(path, assignments[path]) for path in sorted(assignments))


def _inferred_roles(root: Path, files: frozenset[str]) -> tuple[RoleAssignment, ...]:
    mirror_sources = {
        PurePosixPath(path).parts[1]
        for path in files
        if len(PurePosixPath(path).parts) >= 4
        and PurePosixPath(path).parts[0] == "sources"
        and PurePosixPath(path).parts[2] == "mirror"
    }
    source_kinds = {source_id: _source_kind(root, source_id) for source_id in mirror_sources}
    assignments: list[RoleAssignment] = []
    for path in sorted(files):
        parts = PurePosixPath(path).parts
        role = PayloadRole.METADATA
        if len(parts) >= 4 and parts[0] == "sources":
            if parts[2] == "warc":
                role = (
                    PayloadRole.PRESERVATION_ORIGINAL
                    if len(parts) == 4 and _is_warc_container(path)
                    else PayloadRole.CAPTURE_DERIVATIVE
                )
            elif parts[2] == "mirror":
                role = (
                    PayloadRole.PRESERVATION_ORIGINAL
                    if source_kinds[parts[1]] == "http-file"
                    else PayloadRole.CAPTURE_DERIVATIVE
                )
        assignments.append(RoleAssignment(path, role))
    return tuple(assignments)


def _source_kind(root: Path, source_id: str) -> str:
    candidates = (
        root / "sources" / source_id / "metadata" / "resolved-source.json",
        root / "metadata" / "resolved-collection.json",
    )
    for candidate in candidates:
        try:
            value = json.loads(_read_regular(candidate).decode("utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PayloadRoleError(f"cannot infer source role from {candidate}: {exc}") from exc
        if candidate.name == "resolved-source.json":
            kind = value.get("kind") if isinstance(value, dict) else None
        else:
            sources = value.get("sources") if isinstance(value, dict) else None
            matching = [
                source
                for source in sources or []
                if isinstance(source, dict) and source.get("id") == source_id
            ] if isinstance(sources, list) else []
            kind = matching[0].get("kind") if len(matching) == 1 else None
        if kind in {"web", "http-file"}:
            return kind
        raise PayloadRoleError(f"cannot infer supported kind for source {source_id!r}")
    raise PayloadRoleError(f"cannot infer role for source mirror without metadata: {source_id}")


def _capture_files(root: Path) -> frozenset[str]:
    files: set[str] = set()
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in (*directory_names, *file_names):
            path = base / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise PayloadRoleError(f"capture contains a symlink: {path}")
            if stat.S_ISREG(info.st_mode):
                files.add(path.relative_to(root).as_posix())
            elif not stat.S_ISDIR(info.st_mode):
                raise PayloadRoleError(f"capture contains unsupported object: {path}")
    return frozenset(files)


def _safe_capture_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or value != path.as_posix()
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise PayloadRoleError(f"unsafe capture path: {value!r}")
    return value


def _is_warc_container(path: str) -> bool:
    lower = path.lower()
    return lower.endswith(".warc") or lower.endswith(".warc.gz")


def _shareable_metadata_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    if path in {
        "metadata/environment.json",
        "metadata/resolved-collection.json",
        "metadata/recipe-bundle-manifest.json",
    }:
        return True
    if path.startswith("metadata/recipe-bundle/"):
        return True
    if len(parts) >= 3 and parts[0] == "sources":
        return parts[2:] in {
            ("seeds.txt",),
            ("metadata", "command.json"),
            ("metadata", "redirects.json"),
            ("metadata", "resolved-source.json"),
            ("metadata", "result.json"),
        } or (parts[2] == "warc" and path.lower().endswith(".cdx"))
    return False


def _payload_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return len(parts) >= 4 and parts[0] == "sources" and parts[2] in {"mirror", "warc"}


def _shareable_payload_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return _payload_path(path) and "private" not in parts and "logs" not in parts


def _fixity_objects(root: Path, manifest_sha256: str) -> tuple[dict[str, str], tuple[str, ...]]:
    try:
        value = json.loads(_read_regular(root / MANIFEST_NAME).decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PayloadRoleError(f"invalid fixity manifest: {exc}") from exc
    entries = value.get("files") if isinstance(value, dict) else None
    if not isinstance(entries, list):
        raise PayloadRoleError("invalid fixity manifest object list")
    files = {MANIFEST_NAME: manifest_sha256}
    directories: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise PayloadRoleError("invalid fixity manifest object")
        path = entry.get("path")
        object_type = entry.get("type")
        digest = entry.get("sha256")
        if not isinstance(path, str) or not isinstance(digest, str):
            raise PayloadRoleError("invalid fixity manifest object identity")
        if object_type == "file":
            files[path] = digest
        elif object_type == "directory":
            directories.append(path)
        else:
            raise PayloadRoleError("invalid fixity manifest object type")
    return files, tuple(sorted(directories))


def _export_profile(value: ExportProfile | str) -> ExportProfile:
    try:
        return ExportProfile(value)
    except ValueError as exc:
        raise PayloadRoleError(f"unsupported export profile: {value!r}") from exc


def _read_regular(path: Path) -> bytes:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise PayloadRoleError(f"expected a regular file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise PayloadRoleError(f"file changed while opening: {path}")
        return stream.read()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    info = path.lstat()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            info.st_dev,
            info.st_ino,
        ):
            raise PayloadRoleError(f"file changed while hashing: {path}")
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
