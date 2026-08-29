"""Safely preserve and verify versioned recipe bundles."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Any, Iterable


BUNDLE_MANIFEST_SCHEMA_VERSION = 1
MAX_BUNDLE_FILES = 1_000
MAX_BUNDLE_FILE_SIZE = 16 * 1024 * 1024
MAX_BUNDLE_TOTAL_SIZE = 64 * 1024 * 1024
TRANSIENT_DIRECTORY_NAMES = frozenset({"__pycache__"})
TRANSIENT_FILE_NAMES = frozenset({".DS_Store"})
TRANSIENT_FILE_SUFFIXES = frozenset({".pyc", ".pyo"})


class RecipeBundleError(RuntimeError):
    """Raised when a recipe bundle is malformed, unsafe, or unstable."""


@dataclass(frozen=True)
class BundleFile:
    path: str
    source: Path
    size: int
    sha256: str

    def manifest_entry(self) -> dict[str, Any]:
        return {"path": self.path, "size": self.size, "sha256": self.sha256}


@dataclass(frozen=True)
class RecipeBundle:
    root: Path
    files: tuple[BundleFile, ...]
    sha256: str

    def manifest(
        self,
        *,
        recipe_api: int | None,
        collection_id: str,
    ) -> dict[str, Any]:
        return {
            "schema_version": BUNDLE_MANIFEST_SCHEMA_VERSION,
            "recipe_api": recipe_api,
            "collection_id": collection_id,
            "files": [item.manifest_entry() for item in self.files],
            "bundle_sha256": self.sha256,
        }


def scan_recipe_directory(
    root: Path,
    *,
    max_files: int = MAX_BUNDLE_FILES,
    max_file_size: int = MAX_BUNDLE_FILE_SIZE,
    max_total_size: int = MAX_BUNDLE_TOTAL_SIZE,
) -> RecipeBundle:
    """Recursively inventory every non-transient regular file below ``root``."""
    root = _validated_root(root)
    paths: list[Path] = []

    def visit(directory: Path) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise RecipeBundleError(f"cannot scan recipe bundle directory {directory}: {exc}") from exc
        for entry in entries:
            path = Path(entry.path)
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise RecipeBundleError(f"cannot inspect recipe bundle object {path}: {exc}") from exc
            relative = path.relative_to(root).as_posix()
            _validate_relative_path(relative)
            if stat.S_ISLNK(info.st_mode):
                raise RecipeBundleError(f"recipe bundle contains a symlink: {relative}")
            if stat.S_ISDIR(info.st_mode):
                if entry.name not in TRANSIENT_DIRECTORY_NAMES:
                    visit(path)
                continue
            if not stat.S_ISREG(info.st_mode):
                raise RecipeBundleError(f"recipe bundle contains a special file: {relative}")
            if _is_transient_file(entry.name):
                continue
            if len(paths) >= max_files:
                raise RecipeBundleError(f"recipe bundle exceeds {max_files} files")
            paths.append(path)

    visit(root)
    return _scan_files(
        root,
        paths,
        max_files=max_files,
        max_file_size=max_file_size,
        max_total_size=max_total_size,
    )


def scan_declared_assets(
    root: Path,
    relative_paths: Iterable[str],
    *,
    max_files: int = MAX_BUNDLE_FILES,
    max_file_size: int = MAX_BUNDLE_FILE_SIZE,
    max_total_size: int = MAX_BUNDLE_TOTAL_SIZE,
) -> RecipeBundle:
    """Inventory only explicitly declared files below ``root``."""
    root = _validated_root(root)
    paths: dict[str, Path] = {}
    for value in relative_paths:
        _validate_relative_path(value)
        relative = PurePosixPath(value)
        if _is_transient_file(relative.name) or any(
            part in TRANSIENT_DIRECTORY_NAMES for part in relative.parts[:-1]
        ):
            continue
        path = root.joinpath(*relative.parts)
        _validate_path_components(root, path)
        paths[value] = path
    return _scan_files(
        root,
        (paths[name] for name in sorted(paths)),
        max_files=max_files,
        max_file_size=max_file_size,
        max_total_size=max_total_size,
    )


def copy_bundle(bundle: RecipeBundle, destination: Path) -> None:
    """Copy an inventoried bundle and reject source changes during copying."""
    if destination.is_symlink() or destination.exists():
        raise RecipeBundleError(f"recipe bundle destination already exists: {destination}")
    destination.mkdir()
    for item in bundle.files:
        target = destination.joinpath(*PurePosixPath(item.path).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        copied = 0
        try:
            _validate_path_components(bundle.root, item.source)
            source_info = item.source.lstat()
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(item.source, flags)
            with os.fdopen(descriptor, "rb") as source, target.open("xb") as output:
                opened = os.fstat(source.fileno())
                if not stat.S_ISREG(opened.st_mode) or (
                    opened.st_dev,
                    opened.st_ino,
                ) != (source_info.st_dev, source_info.st_ino):
                    raise RecipeBundleError(f"recipe bundle file changed while opening: {item.path}")
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    output.write(block)
                    digest.update(block)
                    copied += len(block)
                final = os.fstat(source.fileno())
        except OSError as exc:
            raise RecipeBundleError(f"cannot copy recipe bundle file {item.path}: {exc}") from exc
        if (
            copied != item.size
            or digest.hexdigest() != item.sha256
            or (opened.st_size, opened.st_mtime_ns) != (final.st_size, final.st_mtime_ns)
        ):
            raise RecipeBundleError(f"recipe bundle file changed while copying: {item.path}")


def verify_bundle_manifest(
    bundle_root: Path,
    manifest_path: Path,
    *,
    expected_collection_id: str | None = None,
    expected_recipe_api: int | None = None,
) -> tuple[RecipeBundle, dict[str, Any]]:
    """Validate a bundle manifest and verify every bounded bundle file."""
    try:
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise RecipeBundleError(f"recipe bundle manifest is not a regular file: {manifest_path}")
        raw = manifest_path.read_bytes()
        if len(raw) > 4 * 1024 * 1024:
            raise RecipeBundleError("recipe bundle manifest exceeds 4 MiB")
        manifest = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RecipeBundleError(f"cannot read recipe bundle manifest {manifest_path}: {exc}") from exc
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version",
        "recipe_api",
        "collection_id",
        "files",
        "bundle_sha256",
    }:
        raise RecipeBundleError("recipe bundle manifest has invalid fields")
    if manifest["schema_version"] != BUNDLE_MANIFEST_SCHEMA_VERSION or type(
        manifest["schema_version"]
    ) is not int:
        raise RecipeBundleError("recipe bundle manifest has an unsupported schema version")
    recipe_api = manifest["recipe_api"]
    if recipe_api is not None and type(recipe_api) is not int:
        raise RecipeBundleError("recipe bundle manifest has an invalid recipe API")
    collection_id = manifest["collection_id"]
    if not isinstance(collection_id, str) or not collection_id:
        raise RecipeBundleError("recipe bundle manifest has an invalid collection ID")
    if expected_collection_id is not None and collection_id != expected_collection_id:
        raise RecipeBundleError("recipe bundle manifest collection ID does not match the collection")
    if expected_recipe_api is not None and recipe_api != expected_recipe_api:
        raise RecipeBundleError("recipe bundle manifest recipe API does not match the collection")
    expected_entries = _validate_manifest_entries(manifest["files"])
    expected_digest = manifest["bundle_sha256"]
    if not _is_sha256(expected_digest):
        raise RecipeBundleError("recipe bundle manifest has an invalid bundle SHA-256")
    if canonical_bundle_sha256(expected_entries) != expected_digest:
        raise RecipeBundleError("recipe bundle manifest has a mismatched canonical digest")
    bundle = scan_recipe_directory(bundle_root)
    if [item.manifest_entry() for item in bundle.files] != expected_entries:
        raise RecipeBundleError("recipe bundle contents do not match the manifest")
    if bundle.sha256 != expected_digest:
        raise RecipeBundleError("recipe bundle digest does not match the manifest")
    return bundle, manifest


def canonical_bundle_sha256(entries: Iterable[dict[str, Any]]) -> str:
    """Hash canonical JSON for deterministic ordered bundle entries."""
    ordered = sorted(entries, key=lambda entry: entry["path"])
    payload = json.dumps(
        {"files": ordered},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validated_root(root: Path) -> Path:
    expanded = root.expanduser()
    try:
        info = expanded.lstat()
    except OSError as exc:
        raise RecipeBundleError(f"recipe bundle root is unavailable: {expanded}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RecipeBundleError(f"recipe bundle root is not a regular directory: {expanded}")
    return expanded.resolve()


def _scan_files(
    root: Path,
    paths: Iterable[Path],
    *,
    max_files: int,
    max_file_size: int,
    max_total_size: int,
) -> RecipeBundle:
    files: list[BundleFile] = []
    total_size = 0
    for path in paths:
        relative = path.relative_to(root).as_posix()
        _validate_relative_path(relative)
        if len(files) >= max_files:
            raise RecipeBundleError(f"recipe bundle exceeds {max_files} files")
        try:
            _validate_path_components(root, path)
            info = path.lstat()
        except OSError as exc:
            raise RecipeBundleError(f"cannot inspect recipe bundle file {relative}: {exc}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise RecipeBundleError(f"recipe bundle contains a symlink: {relative}")
        if not stat.S_ISREG(info.st_mode):
            raise RecipeBundleError(f"recipe bundle contains a special file: {relative}")
        if info.st_size > max_file_size:
            raise RecipeBundleError(
                f"recipe bundle file exceeds {max_file_size} bytes: {relative}"
            )
        total_size += info.st_size
        if total_size > max_total_size:
            raise RecipeBundleError(f"recipe bundle exceeds {max_total_size} total bytes")
        files.append(BundleFile(relative, path, info.st_size, _hash_regular_file(path, info)))
    files.sort(key=lambda item: item.path)
    entries = [item.manifest_entry() for item in files]
    return RecipeBundle(root, tuple(files), canonical_bundle_sha256(entries))


def _hash_regular_file(path: Path, expected: os.stat_result) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or (
                opened.st_dev,
                opened.st_ino,
            ) != (expected.st_dev, expected.st_ino):
                raise RecipeBundleError(f"recipe bundle file changed while opening: {path}")
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
            final = os.fstat(stream.fileno())
    except OSError as exc:
        raise RecipeBundleError(f"cannot hash recipe bundle file {path}: {exc}") from exc
    if (opened.st_size, opened.st_mtime_ns) != (final.st_size, final.st_mtime_ns):
        raise RecipeBundleError(f"recipe bundle file changed while hashing: {path}")
    return digest.hexdigest()


def _validate_manifest_entries(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise RecipeBundleError("recipe bundle manifest files must be an array")
    entries: list[dict[str, Any]] = []
    previous: str | None = None
    for index, entry in enumerate(value):
        if not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256"}:
            raise RecipeBundleError(f"recipe bundle manifest files[{index}] has invalid fields")
        path = entry["path"]
        if not isinstance(path, str):
            raise RecipeBundleError(f"recipe bundle manifest files[{index}] has an invalid path")
        _validate_relative_path(path)
        if previous is not None and path <= previous:
            raise RecipeBundleError("recipe bundle manifest file paths are not unique POSIX order")
        previous = path
        if type(entry["size"]) is not int or entry["size"] < 0:
            raise RecipeBundleError(f"recipe bundle manifest files[{index}] has an invalid size")
        if not _is_sha256(entry["sha256"]):
            raise RecipeBundleError(f"recipe bundle manifest files[{index}] has an invalid SHA-256")
        entries.append(dict(entry))
    return entries


def _validate_path_components(root: Path, path: Path) -> None:
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        try:
            info = current.lstat()
        except OSError as exc:
            raise RecipeBundleError(f"declared recipe asset is unavailable: {current}: {exc}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise RecipeBundleError(f"declared recipe asset path contains a symlink: {current}")
    if not path.resolve().is_relative_to(root):
        raise RecipeBundleError(f"declared recipe asset escapes its bundle: {path}")


def _validate_relative_path(value: str) -> None:
    if not isinstance(value, str) or not value or "\\" in value or "\n" in value:
        raise RecipeBundleError(f"unsafe recipe bundle path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise RecipeBundleError(f"unsafe recipe bundle path: {value!r}")
    if path.as_posix() != value:
        raise RecipeBundleError(f"non-canonical recipe bundle path: {value!r}")


def _is_transient_file(name: str) -> bool:
    return name in TRANSIENT_FILE_NAMES or Path(name).suffix in TRANSIENT_FILE_SUFFIXES


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )
