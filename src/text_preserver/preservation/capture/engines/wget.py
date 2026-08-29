"""Build deterministic GNU Wget preservation capture commands."""

from __future__ import annotations

from dataclasses import dataclass, replace
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
    request_urls: tuple[str, ...]

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
            "request_urls": list(self.request_urls),
        }

    def to_portable_dict(self) -> dict[str, Any]:
        """Return shareable command provenance without machine-local paths."""
        argv = ("wget", *self.argv[1:])
        return {
            "source_id": self.source_id,
            "source_kind": self.source_kind,
            "required": self.required,
            "success_exit_codes": list(self.success_exit_codes),
            "working_directory": ".",
            "argv": list(argv),
            "shell_command": shlex.join(argv),
            "required_directories": [
                path.relative_to(self.working_directory).as_posix()
                if path != self.working_directory
                else "."
                for path in self.required_directories
            ],
            "request_urls": list(self.request_urls),
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
        "--output-file=logs/wget.log",
        "--hsts-file=metadata/wget-hsts",
        "--directory-prefix=mirror",
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
                "--warc-file=warc/capture",
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
        request_urls=source.seeds,
    )


def build_redirect_command(
    command: WgetCommand,
    target_url: str,
    sequence: int,
    *,
    remaining_quota: int | None = None,
) -> WgetCommand:
    """Build one non-recursive, non-following request for a reviewed redirect."""
    argv = list(command.argv[: -len(command.request_urls)])
    argv = [
        value
        for value in argv
        if value not in {"--recursive", "--page-requisites", "--convert-links"}
        and not value.startswith("--level=")
    ]
    replacements = {
        "--output-file=": f"--output-file=logs/wget-redirect-{sequence:04d}.log",
        "--warc-file=": f"--warc-file=warc/capture-redirect-{sequence:04d}",
    }
    for index, value in enumerate(argv):
        for prefix, replacement in replacements.items():
            if value.startswith(prefix):
                argv[index] = replacement
                break
        if remaining_quota is not None and value.startswith("--quota="):
            argv[index] = f"--quota={remaining_quota}"
    argv.append(target_url)
    return replace(command, argv=tuple(argv), request_urls=(target_url,))


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
