"""Local environment checks for preservation captures."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from pathlib import Path
import shutil
import subprocess
import sys

from text_preserver.config import ConfigError, load_config


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    ok: bool
    detail: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def inspect_environment(config_path: str | Path) -> list[DoctorCheck]:
    """Inspect configuration and local capture dependencies without changing them."""
    checks = [
        DoctorCheck(
            "python",
            sys.version_info >= (3, 11),
            f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        )
    ]
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        checks.append(DoctorCheck("configuration", False, str(exc)))
        return checks

    enabled = sum(collection.enabled for collection in config.collections)
    source_count = sum(len(collection.sources) for collection in config.collections)
    checks.append(
        DoctorCheck(
            "configuration",
            True,
            f"{len(config.collections)} collection(s), {enabled} enabled, {source_count} source(s)",
        )
    )
    checks.extend(_inspect_wget())
    for name, path in (
        ("archive root", config.project.archive_root),
        ("derived root", config.project.derived_root),
        ("workspace root", config.project.workspace_root),
    ):
        checks.append(_inspect_root(name, path))
    checks.append(_inspect_disk(config.project.archive_root))
    return checks


def _inspect_wget() -> list[DoctorCheck]:
    executable = shutil.which("wget")
    if executable is None:
        return [DoctorCheck("GNU Wget", False, "executable not found on PATH: wget")]
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
        return [DoctorCheck("GNU Wget", False, f"could not execute {executable}: {exc}")]

    version_line = (version.stdout or version.stderr).splitlines()
    identity = version_line[0] if version_line else "no version output"
    is_gnu = version.returncode == 0 and "GNU Wget" in identity
    checks = [DoctorCheck("GNU Wget", is_gnu, identity)]
    required_options = ("--warc-file", "--warc-cdx", "--warc-tempdir")
    missing = [option for option in required_options if option not in help_result.stdout]
    checks.append(
        DoctorCheck(
            "WARC support",
            help_result.returncode == 0 and not missing,
            "available" if not missing else f"missing options: {', '.join(missing)}",
        )
    )
    return checks


def _inspect_root(name: str, path: Path) -> DoctorCheck:
    if path.exists() and not path.is_dir():
        return DoctorCheck(name, False, f"not a directory: {path}")
    parent = _nearest_existing_parent(path)
    if not parent.is_dir():
        return DoctorCheck(name, False, f"path component is not a directory: {parent}")
    writable = os.access(parent, os.W_OK | os.X_OK)
    detail = str(path) if path.exists() else f"{path} (parent {parent})"
    return DoctorCheck(name, writable, detail)


def _inspect_disk(path: Path) -> DoctorCheck:
    parent = _nearest_existing_parent(path)
    if not parent.is_dir():
        return DoctorCheck("free space", False, f"path component is not a directory: {parent}")
    try:
        free = shutil.disk_usage(parent).free
    except OSError as exc:
        return DoctorCheck("free space", False, str(exc))
    return DoctorCheck("free space", free > 0, f"{_format_bytes(free)} available at {parent}")


def _nearest_existing_parent(path: Path) -> Path:
    candidate = path
    while not candidate.exists():
        candidate = candidate.parent
    return candidate


def _format_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    raise AssertionError("unreachable")
