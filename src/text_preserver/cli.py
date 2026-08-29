"""Command-line interface for text-preserver."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence, cast
import webbrowser

from text_preserver import __version__
from text_preserver.access.catalogue import (
    build_catalogue,
    current_catalogue_index,
    search_catalogue,
)
from text_preserver.access.reader import build_static_reader, current_reader_index
from text_preserver.access.wacz import (
    WaczError,
    WaczMetadata,
    create_wacz,
    validate_wacz,
)
from text_preserver.preservation.capture import (
    CaptureExecutionError,
    CapturePlanError,
    execute_capture,
    plan_capture,
)
from text_preserver.config import CollectionConfig, ConfigError, load_config
from text_preserver.doctor import inspect_environment
from text_preserver.derived import AnalysisError
from text_preserver.preservation.fixity import verify_capture
from text_preserver.preservation.bagit import BagItError, create_bag, validate_bag
from text_preserver.preservation.payload_roles import (
    PayloadRoleError,
    export_policy,
    load_verified_capture,
    preservation_warc_paths,
)
from text_preserver.preservation.validation import analyze_preservation
from text_preserver.lifecycle import collection_lifecycle_status


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="text-preserver",
        description="Preserve vulnerable digital text collections and their context.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser(
        "doctor",
        help="validate configuration and inspect local capture dependencies",
    )
    doctor.add_argument(
        "-c",
        "--config",
        type=Path,
        default=Path("collections.toml"),
        help="TOML configuration file (default: collections.toml)",
    )
    doctor.add_argument("--json", action="store_true", help="emit machine-readable results")

    collections = subparsers.add_parser(
        "collections",
        help="inspect configured collections",
    )
    collection_commands = collections.add_subparsers(dest="collections_command", required=True)
    collections_list = collection_commands.add_parser("list", help="list configured collections")
    _add_config_argument(collections_list)
    collections_list.add_argument("--json", action="store_true", help="emit machine-readable results")
    collections_show = collection_commands.add_parser("show", help="show one resolved collection")
    collections_show.add_argument("collection_id", help="configured collection ID")
    _add_config_argument(collections_show)
    collections_show.add_argument("--json", action="store_true", help="emit machine-readable results")
    collections_status = collection_commands.add_parser(
        "status", help="show independent collection lifecycle states"
    )
    collections_status.add_argument("collection_id", help="configured collection ID")
    _add_config_argument(collections_status)
    collections_status.add_argument("--json", action="store_true", help="emit machine-readable results")

    capture = subparsers.add_parser(
        "capture",
        help="capture a configured collection",
    )
    capture.add_argument("collection_id", help="configured collection ID")
    capture.add_argument(
        "-c",
        "--config",
        type=Path,
        default=Path("collections.toml"),
        help="TOML configuration file (default: collections.toml)",
    )
    capture.add_argument(
        "--source",
        dest="source_ids",
        action="append",
        default=[],
        help="plan only this source; repeatable",
    )
    capture.add_argument(
        "--capture-id",
        help="explicit capture ID for inspecting final paths",
    )
    capture.add_argument(
        "--dry-run",
        action="store_true",
        help="show resolved commands without writing files or making requests",
    )
    capture.add_argument("--note", help="operator note preserved with the capture")
    capture.add_argument("--json", action="store_true", help="emit machine-readable plan")

    verify = subparsers.add_parser(
        "verify",
        help="verify a finalized capture against its SHA-256 manifest",
    )
    verify.add_argument(
        "path",
        type=Path,
        help="capture directory or collection with LATEST-ACQUIRED (or legacy LATEST)",
    )
    verify.add_argument("--json", action="store_true", help="emit machine-readable results")

    analyze = subparsers.add_parser("analyze", help="analyze preserved collection content")
    analyze_commands = analyze.add_subparsers(dest="analyze_command", required=True)
    preservation = analyze_commands.add_parser(
        "preservation",
        help="run collection-specific completeness analysis",
    )
    _add_validation_arguments(preservation)

    validate = subparsers.add_parser(
        "validate", help="run collection-specific preservation validation"
    )
    _add_validation_arguments(validate)

    derive = subparsers.add_parser("derive", help="build access copies from preserved content")
    derive_commands = derive.add_subparsers(dest="derive_command", required=True)
    reader = derive_commands.add_parser("reader", help="build a collection-specific static reader")
    reader.add_argument("collection_id", help="configured collection ID")
    reader.add_argument(
        "capture_path",
        nargs="?",
        type=Path,
        help="capture directory; defaults to LATEST-ACQUIRED (or legacy LATEST)",
    )
    _add_config_argument(reader)
    reader.add_argument("--json", action="store_true", help="emit machine-readable result")
    catalogue = derive_commands.add_parser(
        "catalogue", help="build a common catalogue and SQLite FTS5 index"
    )
    catalogue.add_argument(
        "collection_ids",
        nargs="*",
        help="collections to include; defaults to all enabled collections",
    )
    _add_config_argument(catalogue)
    catalogue.add_argument("--json", action="store_true", help="emit machine-readable result")

    open_command = subparsers.add_parser("open", help="open a derived access copy")
    open_commands = open_command.add_subparsers(dest="open_command", required=True)
    open_reader = open_commands.add_parser("reader", help="open the current static reader")
    open_reader.add_argument("collection_id", help="configured collection ID")
    _add_config_argument(open_reader)
    open_reader.add_argument(
        "--print-only",
        action="store_true",
        help="print the index path without launching a browser",
    )
    open_reader.add_argument("--json", action="store_true", help="emit the index path as JSON")
    open_catalogue = open_commands.add_parser(
        "catalogue", help="open the current common collection catalogue"
    )
    _add_config_argument(open_catalogue)
    open_catalogue.add_argument(
        "--print-only",
        action="store_true",
        help="print the index path without launching a browser",
    )
    open_catalogue.add_argument("--json", action="store_true", help="emit the index path as JSON")

    search = subparsers.add_parser("search", help="query the current SQLite FTS5 access index")
    search.add_argument("query", help="SQLite FTS5 query")
    _add_config_argument(search)
    search.add_argument("--collection", dest="collection_ids", action="append", default=[])
    search.add_argument("--language", dest="languages", action="append", default=[])
    search.add_argument("--kind", dest="kinds", action="append", default=[])
    search.add_argument("--item-type", dest="item_types", action="append", default=[])
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--json", action="store_true", help="emit machine-readable results")

    export = subparsers.add_parser("export", help="create or validate interoperable exports")
    export_commands = export.add_subparsers(dest="export_command", required=True)
    export_bagit = export_commands.add_parser("bagit", help="export a verified capture as BagIt")
    _add_export_creation_arguments(export_bagit)
    export_wacz = export_commands.add_parser("wacz", help="derive a replayable WACZ from capture WARCs")
    _add_export_creation_arguments(export_wacz)
    export_wacz.add_argument("--title", help="package title; defaults to the collection and capture IDs")
    export_wacz.add_argument(
        "--description",
        default="Offline replay export derived from a verified text-preserver capture.",
        help="package description",
    )
    export_wacz.add_argument("--main-page-url", help="preferred replay start URL")
    validate_bagit_parser = export_commands.add_parser(
        "validate-bagit", help="validate an existing BagIt package"
    )
    validate_bagit_parser.add_argument("path", type=Path, help="BagIt directory")
    validate_bagit_parser.add_argument("--json", action="store_true", help="emit machine-readable results")
    validate_wacz_parser = export_commands.add_parser(
        "validate-wacz", help="validate an existing WACZ package"
    )
    validate_wacz_parser.add_argument("path", type=Path, help="WACZ file")
    validate_wacz_parser.add_argument("--json", action="store_true", help="emit machine-readable results")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "doctor":
        checks = inspect_environment(args.config)
        if args.json:
            print(json.dumps({"checks": [check.to_dict() for check in checks]}, indent=2))
        else:
            for check in checks:
                marker = "ok" if check.ok else "FAIL"
                print(f"[{marker:4}] {check.name}: {check.detail}")
        return 0 if all(check.ok for check in checks) else 1
    if args.command == "collections":
        try:
            config = load_config(args.config)
            if args.collections_command == "list":
                values = [_collection_summary(collection) for collection in config.collections]
                if args.json:
                    print(json.dumps({"collections": values}, indent=2))
                else:
                    for value in values:
                        state = "enabled" if value["enabled"] else "disabled"
                        print(
                            f"{value['id']}\t{state}\t{value['source_count']} source(s)\t{value['title']}"
                        )
                return 0
            if args.collections_command == "status":
                value = cast(
                    dict[str, dict[str, Any]],
                    collection_lifecycle_status(config, args.collection_id),
                )
                if args.json:
                    print(json.dumps(value, indent=2))
                else:
                    for dimension in ("acquisition", "fixity", "validation", "access"):
                        print(f"{dimension}: {value[dimension]['state']}")
                return 0
            collection = next(
                (item for item in config.collections if item.id == args.collection_id),
                None,
            )
            if collection is None:
                raise ConfigError(f"unknown collection: {args.collection_id}")
            value = _collection_detail(collection)
            if args.json:
                print(json.dumps(value, indent=2))
            else:
                print(f"ID: {value['id']}")
                print(f"Title: {value['title']}")
                print(f"Enabled: {'yes' if value['enabled'] else 'no'}")
                print(f"Recipe: {value['recipe_path'] or 'inline configuration'}")
                print(f"Homepage: {value['homepage'] or ''}")
                print(f"Sources: {len(value['sources'])}")
                for source in value["sources"]:
                    requirement = "required" if source["required"] else "optional"
                    print(f"  {source['id']}: {source['kind']}, {requirement}")
                print(f"Analysis: {json.dumps(value['analysis'], sort_keys=True)}")
            return 0
        except (ConfigError, AnalysisError) as exc:
            print(f"collections error: {exc}", file=sys.stderr)
            return 2
    if args.command == "capture":
        try:
            config = load_config(args.config)
            if args.dry_run:
                plan = plan_capture(
                    config,
                    args.collection_id,
                    source_ids=args.source_ids,
                    capture_id=args.capture_id,
                )
            else:
                capture_result = execute_capture(
                    config,
                    args.collection_id,
                    source_ids=args.source_ids,
                    capture_id=args.capture_id,
                    operator_note=args.note,
                )
        except (ConfigError, CapturePlanError, CaptureExecutionError) as exc:
            print(f"capture error: {exc}", file=sys.stderr)
            return 2
        except KeyboardInterrupt:
            print("capture interrupted", file=sys.stderr)
            return 130
        if args.dry_run:
            if args.json:
                print(json.dumps(plan.to_dict(), indent=2))
            else:
                print(f"Collection: {plan.collection_id}")
                print(f"Capture ID: {plan.capture_id}")
                print(f"Capture directory: {plan.capture_directory}")
                for command in plan.commands:
                    print(f"\nSource: {command.source_id}")
                    print(f"Working directory: {command.working_directory}")
                    print(f"Command: {command.shell_command}")
            return 0
        if args.json:
            print(json.dumps(capture_result.to_dict(), indent=2))
        else:
            print(f"Capture: {capture_result.capture_directory}")
            print(f"Status: {capture_result.status}")
        return 0 if capture_result.status in {"complete", "complete_with_warnings"} else 1
    if args.command == "verify":
        verification_result = verify_capture(args.path)
        if args.json:
            print(json.dumps(verification_result.to_dict(), indent=2))
        else:
            marker = "ok" if verification_result.ok else "FAIL"
            print(
                f"[{marker}] {verification_result.capture_directory}: "
                f"{verification_result.checked_objects} object(s)"
            )
            for error in verification_result.errors:
                print(f"  {error}")
        return 0 if verification_result.ok else 1
    if args.command == "validate" or (
        args.command == "analyze" and args.analyze_command == "preservation"
    ):
        return _run_validation(args, deprecated=args.command == "analyze")
    if args.command == "derive" and args.derive_command == "reader":
        try:
            config = load_config(args.config)
            reader_result = build_static_reader(
                config,
                args.collection_id,
                args.capture_path,
            )
        except (ConfigError, AnalysisError) as exc:
            print(f"reader error: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(reader_result.to_dict(), indent=2))
        else:
            print(f"Capture: {reader_result.capture_directory}")
            print(f"Reader: {reader_result.index_path}")
            if reader_result.current_index_path is not None:
                print(f"Current reader: {reader_result.current_index_path}")
            print(f"Status: {reader_result.metadata['status']}")
            for warning in reader_result.metadata.get("warnings", []):
                print(f"  warning: {warning}")
        return 0 if reader_result.metadata["status"] in {"complete", "complete_with_warnings"} else 1
    if args.command == "derive" and args.derive_command == "catalogue":
        try:
            config = load_config(args.config)
            catalogue_result = build_catalogue(config, args.collection_ids or None)
        except (ConfigError, AnalysisError) as exc:
            print(f"catalogue error: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(catalogue_result.to_dict(), indent=2))
        else:
            print(f"Catalogue: {catalogue_result.index_path}")
            print(f"Search index: {catalogue_result.database_path}")
            print(f"Status: {catalogue_result.metadata['status']}")
            summary = catalogue_result.metadata["summary"]
            print(f"Collections: {summary['available_collection_count']} available, "
                  f"{summary['unavailable_collection_count']} unavailable")
            print(f"Documents: {summary['document_count']}")
            for warning in catalogue_result.metadata.get("warnings", []):
                print(f"  warning: {warning}")
        return (
            0
            if catalogue_result.metadata["status"] in {"complete", "complete_with_warnings"}
            else 1
        )
    if args.command == "open" and args.open_command == "reader":
        try:
            config = load_config(args.config)
            index_path = current_reader_index(config, args.collection_id)
        except (ConfigError, AnalysisError) as exc:
            print(f"open error: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps({"collection_id": args.collection_id, "index_path": str(index_path)}))
        else:
            print(index_path)
        if args.print_only:
            return 0
        if not webbrowser.open(index_path.resolve().as_uri()):
            print("open error: browser launch was not accepted", file=sys.stderr)
            return 2
        return 0
    if args.command == "open" and args.open_command == "catalogue":
        try:
            config = load_config(args.config)
            index_path = current_catalogue_index(config)
        except (ConfigError, AnalysisError) as exc:
            print(f"open error: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps({"index_path": str(index_path)}))
        else:
            print(index_path)
        if args.print_only:
            return 0
        if not webbrowser.open(index_path.resolve().as_uri()):
            print("open error: browser launch was not accepted", file=sys.stderr)
            return 2
        return 0
    if args.command == "search":
        try:
            config = load_config(args.config)
            search_result = search_catalogue(
                config,
                args.query,
                collection_ids=args.collection_ids,
                languages=args.languages,
                kinds=args.kinds,
                item_types=args.item_types,
                limit=args.limit,
            )
        except (ConfigError, AnalysisError) as exc:
            print(f"search error: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(search_result.to_dict(), indent=2))
        else:
            for hit in search_result.hits:
                print(f"{hit.collection_id}\t{hit.title}\t{hit.representation_label}")
                print(f"  {hit.snippet}")
                print(f"  {hit.to_dict()['uri']}")
        return 0
    if args.command == "export":
        return _run_export(args)
    raise AssertionError(f"unhandled command: {args.command}")


def _add_config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=Path("collections.toml"),
        help="TOML configuration file (default: collections.toml)",
    )


def _add_export_creation_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "capture_path",
        type=Path,
        help="finalized capture directory or collection pointer directory",
    )
    parser.add_argument("destination", type=Path, help="new export path")
    parser.add_argument(
        "--profile",
        choices=("private", "public"),
        required=True,
        help="private complete package or public package using the built-in allowlist",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable results")


def _run_export(args: argparse.Namespace) -> int:
    try:
        if args.export_command == "validate-bagit":
            return _report_export_validation(validate_bag(args.path), args.json)
        elif args.export_command == "validate-wacz":
            return _report_export_validation(validate_wacz(args.path), args.json)
        capture = load_verified_capture(args.capture_path)
        policy = export_policy(capture, args.profile)
        creation: Any
        if args.export_command == "bagit":
            creation = create_bag(
                capture,
                args.destination,
                profile=args.profile,
                policy=policy,
            )
        else:
            capture_id = str(capture.metadata.get("capture_id", capture.directory.name))
            collection_id = str(capture.metadata.get("collection_id", "collection"))
            creation = create_wacz(
                capture,
                args.destination,
                warc_paths=preservation_warc_paths(capture),
                profile=args.profile,
                policy=policy,
                metadata=WaczMetadata(
                    title=args.title or f"{collection_id} capture {capture_id}",
                    description=args.description,
                    created=_capture_timestamp(capture.metadata),
                    main_page_url=args.main_page_url,
                ),
            )
    except (BagItError, PayloadRoleError, WaczError, OSError) as exc:
        print(f"export error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(creation.to_dict(), indent=2))
    else:
        print(f"Export: {creation.path}")
        print(f"Profile: {creation.profile.value}")
        print(f"Policy: {creation.policy_identifier}")
    return 0


def _report_export_validation(result: Any, json_output: bool) -> int:
    if json_output:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        marker = "ok" if result.ok else "FAIL"
        print(f"[{marker}] {result.path}")
        for error in result.errors:
            print(f"  {error}")
    return 0 if result.ok else 1


def _capture_timestamp(metadata: Mapping[str, object]) -> str:
    for key in ("ended_at", "started_at"):
        value = metadata.get(key)
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is not None:
                return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _add_validation_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("collection_id", help="configured collection ID")
    parser.add_argument(
        "capture_paths",
        nargs="*",
        type=Path,
        help=(
            "capture directories; defaults to LATEST-ACQUIRED (or legacy LATEST) "
            "and configured source pointers"
        ),
    )
    _add_config_argument(parser)
    parser.add_argument("--json", action="store_true", help="emit machine-readable report")


def _run_validation(args: argparse.Namespace, *, deprecated: bool) -> int:
    if deprecated:
        print("warning: 'analyze preservation' is deprecated; use 'validate'", file=sys.stderr)
    try:
        config = load_config(args.config)
        result = analyze_preservation(
            config,
            args.collection_id,
            args.capture_paths or None,
        )
    except (ConfigError, AnalysisError) as exc:
        print(f"validation error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(f"Capture: {result.capture_directory}")
        print(f"Report: {result.report_path}")
        print(f"Status: {result.status}")
        for error in result.report.get("errors", []):
            print(f"  {error}")
        for warning in result.report.get("warnings", []):
            print(f"  warning: {warning}")
    return 0 if result.status in {"complete", "complete_with_warnings"} else 1


def _collection_summary(collection: CollectionConfig) -> dict[str, Any]:
    return {
        "id": collection.id,
        "title": collection.title,
        "enabled": collection.enabled,
        "source_count": len(collection.sources),
        "recipe_path": str(collection.recipe_path) if collection.recipe_path else None,
        "recipe_api": collection.recipe_api,
    }


def _collection_detail(collection: CollectionConfig) -> dict[str, Any]:
    return {
        **_collection_summary(collection),
        "homepage": collection.homepage,
        "description": collection.description,
        "risk_note": collection.risk_note,
        "rights_note": collection.rights_note,
        "tags": list(collection.tags),
        "capture": _plain(collection.capture),
        "analysis": _plain(collection.analysis),
        "sources": [
            {
                "id": source.id,
                "kind": source.kind,
                "title": source.title,
                "description": source.description,
                "required": source.required,
                "seeds": list(source.seeds),
                "allowed_hosts": list(source.allowed_hosts),
                "capture": _plain(source.capture),
            }
            for source in collection.sources
        ],
    }


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value
