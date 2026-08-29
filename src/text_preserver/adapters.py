"""Load and identify collection-specific adapters."""

from __future__ import annotations

import hashlib
import importlib
from dataclasses import dataclass
from importlib.machinery import ModuleSpec
from pathlib import Path
import sys
import tomllib
from types import ModuleType
from typing import Any, Literal, Mapping

from text_preserver.config import CollectionConfig, Config
from text_preserver.derived import AnalysisError
from text_preserver.preservation.recipe_bundle import (
    RecipeBundleError,
    scan_declared_assets,
    scan_recipe_directory,
    verify_bundle_manifest,
)


AdapterStatus = Literal["complete", "complete_with_warnings", "incomplete"]


@dataclass(frozen=True)
class ValidationContext:
    """Inputs exposed to a recipe API 2 validator."""

    capture_directory: Path
    expected_work_count: int
    required_representation_kinds: tuple[str, ...]
    required_source_ids: tuple[str, ...]


@dataclass(frozen=True)
class ValidationReport:
    """Typed recipe API 2 validation response."""

    status: AdapterStatus
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    details: Mapping[str, Any]


@dataclass(frozen=True)
class ReaderContext:
    """Inputs exposed to a recipe API 2 reader builder."""

    capture_directory: Path
    output_directory: Path
    expected_work_count: int


@dataclass(frozen=True)
class ReaderReport:
    """Typed recipe API 2 reader response."""

    status: AdapterStatus
    summary: Mapping[str, Any]
    warnings: tuple[str, ...]
    files: Mapping[str, str] | None = None


def _adapter_path(
    config: Config,
    collection: CollectionConfig,
    capture_directory: Path,
    value: str,
    *,
    capability: Literal["validator", "reader"],
    prefer_preserved: bool = True,
) -> tuple[Path, str, dict[str, Any]]:
    path = Path(value).expanduser()
    base = collection.recipe_path.parent if collection.recipe_path else config.path.parent
    bundle_root = capture_directory / "metadata" / "recipe-bundle"
    bundle_manifest = capture_directory / "metadata" / "recipe-bundle-manifest.json"
    captured_identity: dict[str, Any] | None = None
    captured_manifest: dict[str, Any] | None = None
    if (
        bundle_root.exists()
        or bundle_root.is_symlink()
        or bundle_manifest.exists()
        or bundle_manifest.is_symlink()
    ):
        try:
            captured_bundle, captured_manifest = verify_bundle_manifest(
                bundle_root,
                bundle_manifest,
                expected_collection_id=collection.id,
            )
        except RecipeBundleError as exc:
            raise AnalysisError(f"invalid captured recipe bundle: {exc}") from exc
        captured_identity = {
            "source": "captured_bundle",
            "recipe_api": captured_manifest["recipe_api"],
            "sha256": captured_bundle.sha256,
        }
    if not path.is_absolute() and prefer_preserved:
        preserved_value = value
        if captured_manifest is not None and captured_manifest["recipe_api"] is not None:
            preserved_value = _captured_recipe_adapter(
                bundle_root,
                captured_manifest,
                capability,
            )
        preserved_path = Path(preserved_value)
        if preserved_path.is_absolute() or ".." in preserved_path.parts:
            raise AnalysisError(
                f"{capability} adapter has an unsafe relative path: {preserved_value}"
            )
        if captured_identity is not None:
            preserved = bundle_root / preserved_path
            resolved_preserved = preserved.resolve()
            if (
                not resolved_preserved.is_relative_to(bundle_root.resolve())
                or preserved.is_symlink()
                or not preserved.is_file()
            ):
                raise AnalysisError(
                    f"captured recipe bundle has no regular {capability} adapter: {preserved_value}"
                )
            return resolved_preserved, "preserved_capture", captured_identity

        legacy_root = capture_directory / "metadata" / "recipe-assets"
        legacy = legacy_root / preserved_path
        resolved_legacy = legacy.resolve()
        if (
            resolved_legacy.is_relative_to(legacy_root.resolve())
            and legacy.is_file()
            and not legacy.is_symlink()
        ):
            return resolved_legacy, "preserved_capture", {
                "source": "legacy_recipe_assets",
                "recipe_api": None,
                "sha256": None,
            }
    if not path.is_absolute():
        path = base / path
    resolved = path.resolve()
    if not resolved.is_relative_to(base.resolve()):
        raise AnalysisError(f"{capability} adapter escapes its configuration directory: {value}")
    if resolved.is_symlink() or not resolved.is_file():
        raise AnalysisError(f"{capability} adapter is not a regular file: {resolved}")
    return resolved, "current_recipe", _current_recipe_identity(config, collection)


def _captured_recipe_adapter(
    bundle_root: Path,
    manifest: Mapping[str, Any],
    capability: Literal["validator", "reader"],
) -> str:
    recipe_path = bundle_root / "collection.toml"
    try:
        if recipe_path.is_symlink() or not recipe_path.is_file():
            raise AnalysisError("captured recipe bundle has no authoritative collection.toml")
        recipe = tomllib.loads(recipe_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise AnalysisError(f"cannot read captured recipe bundle collection.toml: {exc}") from exc
    if not isinstance(recipe, dict) or recipe.get("recipe_api") != manifest["recipe_api"]:
        raise AnalysisError("captured collection.toml has a mismatched recipe API")
    raw_collection = recipe.get("collection")
    if not isinstance(raw_collection, dict) or raw_collection.get("id") != manifest["collection_id"]:
        raise AnalysisError("captured collection.toml has a mismatched collection ID")
    raw_analysis = raw_collection.get("analysis")
    if not isinstance(raw_analysis, dict):
        raise AnalysisError("captured collection.toml has no analysis capabilities")
    if manifest["recipe_api"] == 1:
        key = "inventory_adapter" if capability == "validator" else "reader_adapter"
        adapter = raw_analysis.get(key)
        if capability == "reader" and adapter is None:
            adapter = raw_analysis.get("inventory_adapter")
    elif manifest["recipe_api"] == 2:
        key = "validator_adapter" if capability == "validator" else "reader_adapter"
        adapter = raw_analysis.get(key)
    else:
        raise AnalysisError(
            f"captured collection.toml has unsupported recipe API {manifest['recipe_api']!r}"
        )
    if not isinstance(adapter, str) or not adapter:
        raise AnalysisError(f"captured collection.toml has no {capability} adapter")
    return adapter


def _current_recipe_identity(
    config: Config,
    collection: CollectionConfig,
) -> dict[str, Any]:
    base = collection.recipe_path.parent if collection.recipe_path else config.path.parent
    try:
        if collection.recipe_path is not None:
            bundle = scan_recipe_directory(base)
            source = "current_recipe_bundle"
        else:
            declared = (
                value
                for key in (
                    "inventory_adapter",
                    "validator_adapter",
                    "reader_adapter",
                    "normalizer",
                    "ciao_rules",
                )
                if isinstance((value := collection.analysis.get(key)), str)
            )
            bundle = scan_declared_assets(base, declared)
            source = "current_inline_assets"
    except RecipeBundleError as exc:
        raise AnalysisError(f"cannot inventory current recipe bundle: {exc}") from exc
    identity: dict[str, Any] = {
        "source": source,
        "recipe_api": collection.recipe_api,
        "sha256": bundle.sha256,
    }
    if source == "current_inline_assets":
        identity["files"] = [item.path for item in bundle.files]
    return identity


def _adapter_bundle_root(
    config: Config,
    collection: CollectionConfig,
    adapter_path: Path,
    identity: Mapping[str, Any],
) -> Path | None:
    source = identity.get("source")
    if source == "current_recipe_bundle" and collection.recipe_path is not None:
        return collection.recipe_path.parent.resolve()
    if source == "current_inline_assets":
        return config.path.parent.resolve()
    if source == "captured_bundle":
        for parent in adapter_path.parents:
            if parent.name == "recipe-bundle":
                return parent
    return None


def _validation_report_payload(value: Any, adapter_path: Path) -> dict[str, Any]:
    if not isinstance(value, ValidationReport):
        raise AnalysisError(
            f"validator adapter returned {type(value).__name__}, expected ValidationReport: "
            f"{adapter_path}"
        )
    _validate_status(value.status, "validator", adapter_path)
    _validate_string_tuple(value.errors, "errors", "validator", adapter_path)
    _validate_string_tuple(value.warnings, "warnings", "validator", adapter_path)
    if not isinstance(value.details, Mapping):
        raise AnalysisError(f"validator adapter returned invalid details: {adapter_path}")
    reserved = {"status", "errors", "warnings"} & set(value.details)
    if reserved:
        raise AnalysisError(
            f"validator adapter details contain reserved field {sorted(reserved)[0]!r}: "
            f"{adapter_path}"
        )
    details = dict(value.details)
    _validate_json_value(details, "validator details", adapter_path)
    return {
        **details,
        "status": value.status,
        "errors": list(value.errors),
        "warnings": list(value.warnings),
    }


def _reader_report_payload(value: Any, adapter_path: Path) -> ReaderReport:
    if not isinstance(value, ReaderReport):
        raise AnalysisError(
            f"reader adapter returned {type(value).__name__}, expected ReaderReport: "
            f"{adapter_path}"
        )
    _validate_status(value.status, "reader", adapter_path)
    _validate_string_tuple(value.warnings, "warnings", "reader", adapter_path)
    if not isinstance(value.summary, Mapping):
        raise AnalysisError(f"reader adapter returned invalid summary: {adapter_path}")
    _validate_json_value(value.summary, "reader summary", adapter_path)
    if value.files is not None and (
        not isinstance(value.files, Mapping)
        or not all(
            isinstance(name, str) and isinstance(content, str)
            for name, content in value.files.items()
        )
    ):
        raise AnalysisError(f"reader adapter returned invalid files: {adapter_path}")
    return value


def _validate_status(value: Any, capability: str, adapter_path: Path) -> None:
    if value not in {"complete", "complete_with_warnings", "incomplete"}:
        raise AnalysisError(
            f"{capability} adapter returned an invalid status: {adapter_path}"
        )


def _validate_string_tuple(
    value: Any,
    field: str,
    capability: str,
    adapter_path: Path,
) -> None:
    if type(value) is not tuple or not all(isinstance(item, str) for item in value):
        raise AnalysisError(
            f"{capability} adapter returned invalid {field}: {adapter_path}"
        )


def _validate_json_value(value: Any, label: str, adapter_path: Path) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise AnalysisError(f"{label} is not finite JSON data: {adapter_path}")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item, label, adapter_path)
        return
    if isinstance(value, Mapping) and all(isinstance(key, str) for key in value):
        for item in value.values():
            _validate_json_value(item, label, adapter_path)
        return
    raise AnalysisError(f"{label} is not JSON-compatible: {adapter_path}")


def _load_adapter(path: Path) -> tuple[ModuleType, bytes]:
    directory_identity = hashlib.sha256(
        str(path.parent.resolve()).encode("utf-8")
    ).hexdigest()[:16]
    package_name = f"text_preserver_recipe_{directory_identity}"
    module_identity = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:16]
    name = f"{package_name}.adapter_{module_identity}"
    for loaded_name in tuple(sys.modules):
        if loaded_name == package_name or loaded_name.startswith(f"{package_name}."):
            sys.modules.pop(loaded_name, None)
    importlib.invalidate_caches()
    package = ModuleType(package_name)
    package.__file__ = str(path.parent)
    package.__package__ = package_name
    package.__path__ = [str(path.parent)]
    package.__spec__ = ModuleSpec(package_name, loader=None, is_package=True)
    package.__spec__.submodule_search_locations = [str(path.parent)]
    sys.modules[package_name] = package
    module = ModuleType(name)
    module.__file__ = str(path)
    module.__package__ = package_name
    sys.modules[name] = module
    previous_bytecode_setting = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        source = path.read_bytes()
        code = compile(source, str(path), "exec")
        exec(code, module.__dict__)
    except Exception as exc:
        for loaded_name in tuple(sys.modules):
            if loaded_name == package_name or loaded_name.startswith(f"{package_name}."):
                sys.modules.pop(loaded_name, None)
        raise AnalysisError(f"cannot load adapter {path}: {exc}") from exc
    finally:
        sys.dont_write_bytecode = previous_bytecode_setting
    return module, source
