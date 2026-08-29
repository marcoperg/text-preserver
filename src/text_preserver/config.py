"""Load and validate text-preserver TOML configuration."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlsplit

import tomllib

from text_preserver.recipes import public_recipe_path


SAFE_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")
HOST_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
BYTE_SIZE_RE = re.compile(r"^([1-9][0-9]*)([KkMmGg]?)$")
SUPPORTED_RECIPE_APIS = frozenset({1, 2})

DEFAULT_CAPTURE_SETTINGS: dict[str, Any] = {
    "engine": "wget",
    "recursive": True,
    "level": "inf",
    "page_requisites": True,
    "convert_links": True,
    "adjust_extension": True,
    "content_disposition": False,
    "mirror": True,
    "warc": True,
    "warc_cdx": True,
    "warc_max_size": "1G",
    "robots": True,
    "wait": 1.0,
    "random_wait": True,
    "timeout": 30,
    "tries": 3,
    "retry_connrefused": True,
    "span_hosts": False,
    "no_parent": False,
    "quota": "2G",
    "success_exit_codes": (0,),
}

_CAPTURE_KEYS = {
    "engine",
    "recursive",
    "level",
    "page_requisites",
    "convert_links",
    "adjust_extension",
    "content_disposition",
    "mirror",
    "warc",
    "warc_cdx",
    "warc_max_size",
    "robots",
    "wait",
    "random_wait",
    "timeout",
    "tries",
    "retry_connrefused",
    "span_hosts",
    "no_parent",
    "quota",
    "limit_rate",
    "accept",
    "reject",
    "accept_regex",
    "reject_regex",
    "success_exit_codes",
}
_BOOL_CAPTURE_KEYS = {
    "recursive",
    "page_requisites",
    "convert_links",
    "adjust_extension",
    "content_disposition",
    "mirror",
    "warc",
    "warc_cdx",
    "robots",
    "random_wait",
    "retry_connrefused",
    "span_hosts",
    "no_parent",
}
_STRING_CAPTURE_KEYS = {
    "warc_max_size",
    "quota",
    "limit_rate",
    "accept_regex",
    "reject_regex",
}
_STRING_LIST_CAPTURE_KEYS = {"accept", "reject"}


class ConfigError(ValueError):
    """Raised when configuration is missing, malformed, or unsafe."""


@dataclass(frozen=True)
class ProjectConfig:
    archive_root: Path
    derived_root: Path
    workspace_root: Path
    operator: str
    contact: str
    user_agent: str


@dataclass(frozen=True)
class SourceConfig:
    id: str
    kind: str
    title: str
    description: str
    required: bool
    seeds: tuple[str, ...]
    allowed_hosts: tuple[str, ...]
    reviewed_redirects: tuple[tuple[str, str], ...]
    capture: Mapping[str, Any]


@dataclass(frozen=True)
class CollectionConfig:
    id: str
    title: str
    homepage: str | None
    description: str
    risk_note: str
    rights_note: str
    tags: tuple[str, ...]
    enabled: bool
    sources: tuple[SourceConfig, ...]
    capture: Mapping[str, Any]
    analysis: Mapping[str, Any]
    recipe_path: Path | None
    recipe_api: int | None


@dataclass(frozen=True)
class Config:
    path: Path
    input_bytes: bytes
    project: ProjectConfig
    defaults_capture: Mapping[str, Any]
    collections: tuple[CollectionConfig, ...]
    recipe_input_bytes: Mapping[Path, bytes]


def load_config(path: str | Path) -> Config:
    """Load a TOML configuration file and return its resolved model."""
    config_path = Path(path).expanduser().resolve()
    try:
        input_bytes = config_path.read_bytes()
        raw = tomllib.loads(input_bytes.decode("utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file does not exist: {config_path}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read configuration {config_path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {config_path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(f"configuration is not valid UTF-8: {config_path}") from exc

    _only_keys(raw, {"project", "defaults", "collections", "recipes"}, "configuration")
    project = _load_project(_table(raw.get("project"), "project"), config_path.parent)

    defaults_table = _table(raw.get("defaults", {}), "defaults")
    _only_keys(defaults_table, {"capture"}, "defaults")
    defaults_capture = dict(DEFAULT_CAPTURE_SETTINGS)
    defaults_capture.update(
        _load_capture_settings(defaults_table.get("capture", {}), "defaults.capture")
    )

    collection_values = _list(raw.get("collections", []), "collections")
    collections: list[CollectionConfig] = []
    seen_collection_ids: set[str] = set()
    for index, value in enumerate(collection_values):
        label = f"collections[{index}]"
        collection = _load_collection(
            value,
            label,
            defaults_capture,
            recipe_path=None,
            recipe_api=None,
        )
        if collection.id in seen_collection_ids:
            raise ConfigError(f"{label}.id: duplicate collection ID {collection.id!r}")
        seen_collection_ids.add(collection.id)
        collections.append(collection)

    recipe_values = _string_list(raw.get("recipes", []), "recipes")
    if len(recipe_values) != len(set(recipe_values)):
        raise ConfigError("recipes: duplicate recipe path")
    recipe_input_bytes: dict[Path, bytes] = {}
    for value in recipe_values:
        if value.startswith("public:"):
            try:
                recipe_path = public_recipe_path(value.removeprefix("public:")).resolve()
            except ValueError as exc:
                raise ConfigError(f"recipes: {exc}") from exc
        else:
            recipe_path = _resolve_path(value, config_path.parent)
        recipe_raw, recipe_bytes = _read_toml(recipe_path, "collection recipe")
        _only_keys(recipe_raw, {"recipe_api", "collection"}, f"recipe {recipe_path}")
        recipe_api = recipe_raw.get("recipe_api")
        if type(recipe_api) is not int or recipe_api not in SUPPORTED_RECIPE_APIS:
            raise ConfigError(
                f"recipe {recipe_path}: recipe_api must be one of the supported values "
                "1 or 2"
            )
        label = f"recipe {recipe_path}: collection"
        collection = _load_collection(
            recipe_raw.get("collection"),
            label,
            defaults_capture,
            recipe_path=recipe_path,
            recipe_api=recipe_api,
        )
        if collection.id in seen_collection_ids:
            raise ConfigError(f"{label}.id: duplicate collection ID {collection.id!r}")
        seen_collection_ids.add(collection.id)
        recipe_input_bytes[recipe_path] = recipe_bytes
        collections.append(collection)

    if not collections:
        raise ConfigError("configuration: expected at least one collection or recipe")

    return Config(
        path=config_path,
        input_bytes=input_bytes,
        project=project,
        defaults_capture=MappingProxyType(defaults_capture),
        collections=tuple(collections),
        recipe_input_bytes=MappingProxyType(recipe_input_bytes),
    )


def _read_toml(path: Path, kind: str) -> tuple[dict[str, Any], bytes]:
    try:
        input_bytes = path.read_bytes()
        raw = tomllib.loads(input_bytes.decode("utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"{kind} does not exist: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read {kind} {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{kind} is not valid UTF-8: {path}") from exc
    return raw, input_bytes


def _load_project(value: Mapping[str, Any], base: Path) -> ProjectConfig:
    _only_keys(
        value,
        {
            "archive_root",
            "derived_root",
            "workspace_root",
            "operator",
            "contact",
            "user_agent",
        },
        "project",
    )
    archive_root = _resolve_path(_string(value.get("archive_root"), "project.archive_root"), base)
    derived_root = _resolve_path(
        _string(value.get("derived_root", str(archive_root.parent / "derived")), "project.derived_root"),
        base,
    )
    workspace_root = _resolve_path(
        _string(value.get("workspace_root", str(archive_root.parent / "workspace")), "project.workspace_root"),
        base,
    )
    roots = {
        "archive_root": archive_root,
        "derived_root": derived_root,
        "workspace_root": workspace_root,
    }
    for left_name, left in roots.items():
        for right_name, right in roots.items():
            if left_name >= right_name:
                continue
            if left == right or left.is_relative_to(right) or right.is_relative_to(left):
                raise ConfigError(
                    f"project.{left_name} and project.{right_name} must be separate, non-nested paths"
                )

    return ProjectConfig(
        archive_root=archive_root,
        derived_root=derived_root,
        workspace_root=workspace_root,
        operator=_string(value.get("operator"), "project.operator"),
        contact=_string(value.get("contact"), "project.contact"),
        user_agent=_string(value.get("user_agent"), "project.user_agent"),
    )


def _load_collection(
    value: Any,
    label: str,
    defaults_capture: Mapping[str, Any],
    *,
    recipe_path: Path | None,
    recipe_api: int | None,
) -> CollectionConfig:
    table = _table(value, label)
    _only_keys(
        table,
        {
            "id",
            "title",
            "homepage",
            "description",
            "risk_note",
            "rights_note",
            "tags",
            "enabled",
            "capture",
            "analysis",
            "sources",
        },
        label,
    )
    collection_id = _safe_id(table.get("id"), f"{label}.id")
    collection_capture = dict(defaults_capture)
    collection_capture.update(
        _load_capture_settings(table.get("capture", {}), f"{label}.capture")
    )
    _validate_capture_combination(collection_capture, f"{label}.capture")

    source_values = _list(table.get("sources"), f"{label}.sources", nonempty=True)
    sources: list[SourceConfig] = []
    seen_source_ids: set[str] = set()
    for index, source_value in enumerate(source_values):
        source_label = f"{label}.sources[{index}]"
        source = _load_source(source_value, source_label, collection_capture)
        if source.id in seen_source_ids:
            raise ConfigError(f"{source_label}.id: duplicate source ID {source.id!r}")
        seen_source_ids.add(source.id)
        sources.append(source)

    homepage = table.get("homepage")
    if homepage is not None:
        homepage = _http_url(homepage, f"{label}.homepage", allow_fragment=True)

    analysis = _load_analysis(table.get("analysis", {}), f"{label}.analysis")
    _validate_recipe_capabilities(analysis, recipe_api, f"{label}.analysis")
    reader_source = analysis.get("reader_source")
    if reader_source is not None and reader_source not in seen_source_ids:
        raise ConfigError(
            f"{label}.analysis.reader_source: unknown source ID {reader_source!r}"
        )
    return CollectionConfig(
        id=collection_id,
        title=_string(table.get("title"), f"{label}.title"),
        homepage=homepage,
        description=_string(table.get("description", ""), f"{label}.description", allow_empty=True),
        risk_note=_string(table.get("risk_note", ""), f"{label}.risk_note", allow_empty=True),
        rights_note=_string(table.get("rights_note", ""), f"{label}.rights_note", allow_empty=True),
        tags=tuple(_string_list(table.get("tags", []), f"{label}.tags")),
        enabled=_boolean(table.get("enabled", True), f"{label}.enabled"),
        sources=tuple(sources),
        capture=MappingProxyType(collection_capture),
        analysis=MappingProxyType(analysis),
        recipe_path=recipe_path,
        recipe_api=recipe_api,
    )


def _load_source(
    value: Any,
    label: str,
    collection_capture: Mapping[str, Any],
) -> SourceConfig:
    table = _table(value, label)
    _only_keys(
        table,
        {
            "id",
            "kind",
            "title",
            "description",
            "required",
            "engine",
            "seeds",
            "allowed_hosts",
            "reviewed_redirects",
            "capture",
        },
        label,
    )
    kind = _string(table.get("kind"), f"{label}.kind")
    if kind not in {"web", "http-file"}:
        raise ConfigError(f"{label}.kind: expected 'web' or 'http-file'")

    allowed_hosts = tuple(_host_list(table.get("allowed_hosts"), f"{label}.allowed_hosts"))
    seeds = tuple(
        _http_url(seed, f"{label}.seeds[{index}]", allow_fragment=False)
        for index, seed in enumerate(_list(table.get("seeds"), f"{label}.seeds", nonempty=True))
    )
    allowed_set = set(allowed_hosts)
    for index, seed in enumerate(seeds):
        hostname = urlsplit(seed).hostname
        if hostname not in allowed_set:
            raise ConfigError(
                f"{label}.seeds[{index}]: host {hostname!r} is not in allowed_hosts"
            )

    reviewed_redirects = _reviewed_redirects(
        table.get("reviewed_redirects", []),
        f"{label}.reviewed_redirects",
        allowed_set,
    )

    capture = dict(collection_capture)
    capture.update(_load_capture_settings(table.get("capture", {}), f"{label}.capture"))
    if "engine" in table:
        engine = _string(table["engine"], f"{label}.engine")
        if engine != "wget":
            raise ConfigError(f"{label}.engine: only 'wget' is currently supported")
        capture["engine"] = engine
    _validate_capture_combination(capture, f"{label}.capture")
    if reviewed_redirects and not capture["warc"]:
        raise ConfigError(f"{label}.reviewed_redirects: requires WARC capture")
    if kind == "http-file" and capture["recursive"]:
        raise ConfigError(f"{label}.capture.recursive: must be false for an http-file source")

    return SourceConfig(
        id=_safe_id(table.get("id"), f"{label}.id"),
        kind=kind,
        title=_string(table.get("title"), f"{label}.title"),
        description=_string(table.get("description", ""), f"{label}.description", allow_empty=True),
        required=_boolean(table.get("required", True), f"{label}.required"),
        seeds=seeds,
        allowed_hosts=allowed_hosts,
        reviewed_redirects=reviewed_redirects,
        capture=MappingProxyType(capture),
    )


def _load_analysis(value: Any, label: str) -> dict[str, Any]:
    table = _table(value, label)
    keys = {
        "inventory_adapter",
        "validator_adapter",
        "reader_adapter",
        "normalizer",
        "ciao_rules",
        "expected_work_count",
        "prefer_preserved_adapter",
        "required_representation_kinds",
        "reader_source",
        "reader_timeout",
    }
    _only_keys(table, keys, label)
    result: dict[str, Any] = {}
    for key in {
        "inventory_adapter",
        "validator_adapter",
        "reader_adapter",
        "normalizer",
        "ciao_rules",
        "reader_source",
    }:
        if key in table:
            result[key] = _string(table[key], f"{label}.{key}")
    if "expected_work_count" in table:
        count = table["expected_work_count"]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ConfigError(f"{label}.expected_work_count: expected a non-negative integer")
        result["expected_work_count"] = count
    if "reader_timeout" in table:
        timeout = table["reader_timeout"]
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not 1 <= timeout <= 900
        ):
            raise ConfigError(f"{label}.reader_timeout: expected a number from 1 to 900")
        result["reader_timeout"] = float(timeout)
    if "prefer_preserved_adapter" in table:
        result["prefer_preserved_adapter"] = _boolean(
            table["prefer_preserved_adapter"],
            f"{label}.prefer_preserved_adapter",
        )
    if "required_representation_kinds" in table:
        result["required_representation_kinds"] = tuple(
            _string_list(
                table["required_representation_kinds"],
                f"{label}.required_representation_kinds",
            )
        )
    return result


def _validate_recipe_capabilities(
    analysis: Mapping[str, Any],
    recipe_api: int | None,
    label: str,
) -> None:
    if recipe_api == 2:
        if "inventory_adapter" in analysis:
            raise ConfigError(
                f"{label}.inventory_adapter: recipe API 2 uses validator_adapter"
            )
        if "validator_adapter" not in analysis:
            raise ConfigError(
                f"{label}.validator_adapter: recipe API 2 must declare a validator capability"
            )
    elif "validator_adapter" in analysis:
        version = "inline collections" if recipe_api is None else "recipe API 1"
        raise ConfigError(f"{label}.validator_adapter: not supported by {version}")


def _load_capture_settings(value: Any, label: str) -> dict[str, Any]:
    table = _table(value, label)
    _only_keys(table, _CAPTURE_KEYS, label)
    result = dict(table)

    for key in _BOOL_CAPTURE_KEYS & result.keys():
        _boolean(result[key], f"{label}.{key}")
    for key in _STRING_CAPTURE_KEYS & result.keys():
        _string(result[key], f"{label}.{key}")
    for key in _STRING_LIST_CAPTURE_KEYS & result.keys():
        result[key] = tuple(_string_list(result[key], f"{label}.{key}"))

    if "engine" in result and result["engine"] != "wget":
        raise ConfigError(f"{label}.engine: only 'wget' is currently supported")
    for key in {"quota", "limit_rate"} & result.keys():
        _byte_size(result[key], f"{label}.{key}")
    if "warc_max_size" in result:
        _byte_size(
            result["warc_max_size"],
            f"{label}.warc_max_size",
            minimum=1024 * 1024,
        )
    if "level" in result:
        level = result["level"]
        if level != "inf" and (
            isinstance(level, bool) or not isinstance(level, int) or level < 0
        ):
            raise ConfigError(f"{label}.level: expected 'inf' or a non-negative integer")
    for key in {"wait", "timeout"} & result.keys():
        number = result[key]
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            raise ConfigError(f"{label}.{key}: expected a number")
        if not math.isfinite(number) or number < 0 or (key == "timeout" and number == 0):
            comparison = "positive" if key == "timeout" else "non-negative"
            raise ConfigError(f"{label}.{key}: expected a {comparison} number")
    if "tries" in result:
        tries = result["tries"]
        if isinstance(tries, bool) or not isinstance(tries, int) or tries < 1:
            raise ConfigError(f"{label}.tries: expected a positive integer")
    if "success_exit_codes" in result:
        codes = _list(result["success_exit_codes"], f"{label}.success_exit_codes")
        if any(isinstance(code, bool) or not isinstance(code, int) or not 0 <= code <= 255 for code in codes):
            raise ConfigError(f"{label}.success_exit_codes: expected integers from 0 to 255")
        if len(codes) != len(set(codes)):
            raise ConfigError(f"{label}.success_exit_codes: duplicate exit code")
        result["success_exit_codes"] = tuple(codes)
    return result


def _validate_capture_combination(settings: Mapping[str, Any], label: str) -> None:
    if not settings["mirror"] and not settings["warc"]:
        raise ConfigError(f"{label}: mirror and warc cannot both be false")
    if settings["warc_cdx"] and not settings["warc"]:
        raise ConfigError(f"{label}.warc_cdx: cannot be true when warc is false")
    if not settings["mirror"]:
        for key in ("convert_links", "adjust_extension", "page_requisites"):
            if settings[key]:
                raise ConfigError(f"{label}.{key}: cannot be true when mirror is false")


def _resolve_path(value: str, base: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _safe_id(value: Any, label: str) -> str:
    identifier = _string(value, label)
    if not SAFE_ID_RE.fullmatch(identifier):
        raise ConfigError(
            f"{label}: expected 1-64 lowercase letters, digits, dots, underscores, or hyphens"
        )
    return identifier


def _byte_size(value: str, label: str, *, minimum: int = 1) -> int:
    match = BYTE_SIZE_RE.fullmatch(value)
    if match is None:
        raise ConfigError(
            f"{label}: expected a positive byte quantity such as 500K, 100M, or 2G"
        )
    multipliers = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}
    size = int(match.group(1)) * multipliers[match.group(2).lower()]
    if size < minimum:
        raise ConfigError(f"{label}: must be at least {minimum} bytes")
    return size


def _host_list(value: Any, label: str) -> list[str]:
    hosts = _string_list(value, label, nonempty=True)
    if len(hosts) != len(set(hosts)):
        raise ConfigError(f"{label}: duplicate host")
    for index, host in enumerate(hosts):
        if host != host.lower() or not _valid_host(host):
            raise ConfigError(
                f"{label}[{index}]: expected a lowercase hostname without scheme, port, path, or wildcard"
            )
    return hosts


def _reviewed_redirects(
    value: Any,
    label: str,
    allowed_hosts: set[str],
) -> tuple[tuple[str, str], ...]:
    values = _list(value, label)
    redirects: list[tuple[str, str]] = []
    seen_from: set[str] = set()
    seen_edges: set[tuple[str, str]] = set()
    for index, item in enumerate(values):
        item_label = f"{label}[{index}]"
        table = _table(item, item_label)
        _only_keys(table, {"from", "to"}, item_label)
        if set(table) != {"from", "to"}:
            missing = "from" if "from" not in table else "to"
            raise ConfigError(f"{item_label}: missing key {missing!r}")
        source_url = _http_url(table["from"], f"{item_label}.from", allow_fragment=False)
        target_url = _http_url(table["to"], f"{item_label}.to", allow_fragment=False)
        edge = (source_url, target_url)
        if source_url == target_url:
            raise ConfigError(f"{item_label}: redirect endpoints must be different")
        for field, url in (("from", source_url), ("to", target_url)):
            hostname = urlsplit(url).hostname
            if hostname not in allowed_hosts:
                raise ConfigError(
                    f"{item_label}.{field}: host {hostname!r} is not in allowed_hosts"
                )
        if edge in seen_edges:
            raise ConfigError(f"{label}: duplicate reviewed redirect edge")
        if source_url in seen_from:
            raise ConfigError(f"{label}: duplicate redirect source URL {source_url!r}")
        seen_edges.add(edge)
        seen_from.add(source_url)
        redirects.append(edge)
    return tuple(redirects)


def _http_url(value: Any, label: str, *, allow_fragment: bool) -> str:
    url = _string(value, label)
    if "\\" in url or any(character.isspace() or ord(character) == 127 for character in url):
        raise ConfigError(f"{label}: URL contains whitespace or an unsafe character")
    try:
        parts = urlsplit(url)
        _ = parts.port
    except ValueError as exc:
        raise ConfigError(f"{label}: invalid URL: {exc}") from exc
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ConfigError(f"{label}: expected an absolute HTTP or HTTPS URL")
    if parts.username is not None or parts.password is not None:
        raise ConfigError(f"{label}: credentials are not allowed in URLs")
    if not allow_fragment and parts.fragment:
        raise ConfigError(f"{label}: URL fragments are not allowed in capture URLs")
    raw_host = parts.netloc.rsplit("@", 1)[-1].split(":", 1)[0]
    if raw_host != raw_host.lower() or not _valid_host(parts.hostname):
        raise ConfigError(f"{label}: expected an ASCII lowercase hostname")
    return url


def _valid_host(host: str) -> bool:
    if len(host) > 253 or host.endswith("."):
        return False
    return all(HOST_LABEL_RE.fullmatch(label) for label in host.split("."))


def _table(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{label}: expected a table")
    return value


def _list(value: Any, label: str, *, nonempty: bool = False) -> list[Any]:
    if not isinstance(value, list):
        raise ConfigError(f"{label}: expected an array")
    if nonempty and not value:
        raise ConfigError(f"{label}: must not be empty")
    return value


def _string(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        requirement = "a string" if allow_empty else "a non-empty string"
        raise ConfigError(f"{label}: expected {requirement}")
    return value


def _string_list(value: Any, label: str, *, nonempty: bool = False) -> list[str]:
    values = _list(value, label, nonempty=nonempty)
    for index, item in enumerate(values):
        _string(item, f"{label}[{index}]")
    return values


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{label}: expected true or false")
    return value


def _only_keys(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ConfigError(f"{label}: unknown key {unknown[0]!r}")
