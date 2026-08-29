"""Build deterministic GNU Wget capture commands."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shlex
from typing import Any, Mapping

from text_preserver.config import ProjectConfig, SourceConfig


@dataclass(frozen=True)
class WgetCommand:
    source_id: str
    source_kind: str
    required: bool
    success_exit_codes: tuple[int, ...]
    working_directory: Path
    argv: tuple[str, ...]
    required_directories: tuple[Path, ...]

    @property
    def shell_command(self) -> str:
        """Return a display-only shell rendering of the argument vector."""
        return shlex.join(self.argv)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_kind": self.source_kind,
            "required": self.required,
            "success_exit_codes": list(self.success_exit_codes),
            "working_directory": str(self.working_directory),
            "argv": list(self.argv),
            "shell_command": self.shell_command,
            "required_directories": [str(path) for path in self.required_directories],
        }


def build_wget_command(
    project: ProjectConfig,
    source: SourceConfig,
    capture_directory: Path,
) -> WgetCommand:
    """Build the exact Wget argument vector for one resolved source."""
    settings = source.capture
    if settings["engine"] != "wget":
        raise ValueError(f"unsupported capture engine: {settings['engine']}")

    source_root = capture_directory / "sources" / source.id
    mirror_root = source_root / "mirror"
    logs_root = source_root / "logs"
    metadata_root = source_root / "metadata"
    warc_root = source_root / "warc"
    warc_temp_root = warc_root / "tmp"

    required_directories = [source_root, logs_root, metadata_root]
    if settings["mirror"]:
        required_directories.append(mirror_root)
    if settings["warc"]:
        required_directories.extend((warc_root, warc_temp_root))

    argv = [
        "wget",
        "--no-config",
        "--no-netrc",
        "--no-proxy",
        "--max-redirect=0",
        f"--user-agent={project.user_agent}",
        f"--output-file={logs_root / 'wget.log'}",
        f"--hsts-file={metadata_root / 'wget-hsts'}",
        f"--directory-prefix={mirror_root}",
        f"--domains={','.join(source.allowed_hosts)}",
        f"--execute=robots={'on' if settings['robots'] else 'off'}",
        f"--wait={_number(settings['wait'])}",
        f"--timeout={_number(settings['timeout'])}",
        f"--tries={settings['tries']}",
    ]

    if settings["random_wait"]:
        argv.append("--random-wait")
    if settings["retry_connrefused"]:
        argv.append("--retry-connrefused")
    if settings["span_hosts"]:
        argv.append("--span-hosts")
    if settings["recursive"]:
        argv.extend(("--recursive", f"--level={settings['level']}"))
    if settings["page_requisites"]:
        argv.append("--page-requisites")
    if settings["convert_links"]:
        argv.append("--convert-links")
    if settings["adjust_extension"]:
        argv.append("--adjust-extension")
    if settings["content_disposition"]:
        argv.append("--content-disposition")
    if settings["no_parent"]:
        argv.append("--no-parent")
    if not settings["mirror"]:
        argv.append("--delete-after")

    if settings["warc"]:
        argv.extend(
            (
                f"--warc-file={warc_root / 'capture'}",
                f"--warc-tempdir={warc_temp_root.relative_to(source_root)}",
                f"--warc-max-size={settings['warc_max_size']}",
            )
        )
        if settings["warc_cdx"]:
            argv.append("--warc-cdx")

    _append_optional_value(argv, settings, "quota", "--quota")
    _append_optional_value(argv, settings, "limit_rate", "--limit-rate")
    _append_optional_list(argv, settings, "accept", "--accept")
    _append_optional_list(argv, settings, "reject", "--reject")
    _append_optional_value(argv, settings, "accept_regex", "--accept-regex")
    _append_optional_value(argv, settings, "reject_regex", "--reject-regex")
    argv.extend(source.seeds)

    return WgetCommand(
        source_id=source.id,
        source_kind=source.kind,
        required=source.required,
        success_exit_codes=tuple(settings["success_exit_codes"]),
        working_directory=source_root,
        argv=tuple(argv),
        required_directories=tuple(required_directories),
    )


def _number(value: int | float) -> str:
    if isinstance(value, float) and value.is_integer():
        return f"{value:.1f}"
    return str(value)


def _append_optional_value(
    argv: list[str],
    settings: Mapping[str, Any],
    key: str,
    option: str,
) -> None:
    if key in settings:
        argv.append(f"{option}={settings[key]}")


def _append_optional_list(
    argv: list[str],
    settings: Mapping[str, Any],
    key: str,
    option: str,
) -> None:
    if key in settings:
        argv.append(f"{option}={','.join(settings[key])}")
