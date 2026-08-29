"""Load and identify collection-specific adapters."""

from __future__ import annotations

import hashlib
import importlib
from importlib.machinery import ModuleSpec
from pathlib import Path
import sys
import tomllib
from types import ModuleType
from typing import Any, Mapping

from text_preserver.config import CollectionConfig, Config
from text_preserver.derived import AnalysisError
from text_preserver.preservation.recipe_bundle import (
    RecipeBundleError,
    scan_declared_assets,
    scan_recipe_directory,
    verify_bundle_manifest,
)


def _adapter_path(
    config: Config,
    collection: CollectionConfig,
    capture_directory: Path,
    value: str,
    *,
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
        if captured_manifest["recipe_api"] != collection.recipe_api:
            raise AnalysisError(
                "captured recipe bundle API does not match the resolved collection"
            )
        captured_identity = {
            "source": "captured_bundle",
            "recipe_api": captured_manifest["recipe_api"],
            "sha256": captured_bundle.sha256,
        }
    if not path.is_absolute() and prefer_preserved:
        preserved_value = value
        if captured_manifest is not None and captured_manifest["recipe_api"] is not None:
            preserved_value = _captured_recipe_adapter(bundle_root, captured_manifest)
        preserved_path = Path(preserved_value)
        if preserved_path.is_absolute() or ".." in preserved_path.parts:
            raise AnalysisError(
                f"inventory adapter has an unsafe relative path: {preserved_value}"
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
                    f"captured recipe bundle has no regular inventory adapter: {preserved_value}"
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
        raise AnalysisError(f"inventory adapter escapes its configuration directory: {value}")
    if resolved.is_symlink() or not resolved.is_file():
        raise AnalysisError(f"inventory adapter is not a regular file: {resolved}")
    return resolved, "current_recipe", _current_recipe_identity(config, collection)


def _captured_recipe_adapter(
    bundle_root: Path,
    manifest: Mapping[str, Any],
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
    adapter = raw_analysis.get("inventory_adapter") if isinstance(raw_analysis, dict) else None
    if not isinstance(adapter, str) or not adapter:
        raise AnalysisError("captured collection.toml has no inventory adapter")
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
    return {
        "source": source,
        "recipe_api": collection.recipe_api,
        "sha256": bundle.sha256,
    }


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
    package.__path__ = [str(path.parent)]  # type: ignore[attr-defined]
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
