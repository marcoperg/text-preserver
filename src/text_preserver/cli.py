"""Command-line interface for text-preserver."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Mapping, Sequence
import webbrowser

from text_preserver import __version__
from text_preserver.analysis import (
    AnalysisError,
    analyze_preservation,
    build_static_reader,
    current_reader_index,
)
from text_preserver.capture import (
    CaptureExecutionError,
    CapturePlanError,
    execute_capture,
    plan_capture,
)
from text_preserver.config import CollectionConfig, ConfigError, load_config
from text_preserver.doctor import inspect_environment
from text_preserver.manifest import verify_capture


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
    verify.add_argument("path", type=Path, help="capture directory or collection with LATEST")
    verify.add_argument("--json", action="store_true", help="emit machine-readable results")

    analyze = subparsers.add_parser("analyze", help="analyze preserved collection content")
    analyze_commands = analyze.add_subparsers(dest="analyze_command", required=True)
    preservation = analyze_commands.add_parser(
        "preservation",
        help="run collection-specific completeness analysis",
    )
    preservation.add_argument("collection_id", help="configured collection ID")
    preservation.add_argument(
        "capture_paths",
        nargs="*",
        type=Path,
        help=(
            "capture directories; defaults to collection LATEST and all "
            "LATEST-SOURCE_ID pointers"
        ),
    )
    _add_config_argument(preservation)
    preservation.add_argument("--json", action="store_true", help="emit machine-readable report")

    derive = subparsers.add_parser("derive", help="build access copies from preserved content")
    derive_commands = derive.add_subparsers(dest="derive_command", required=True)
    reader = derive_commands.add_parser("reader", help="build a collection-specific static reader")
    reader.add_argument("collection_id", help="configured collection ID")
    reader.add_argument(
        "capture_path",
        nargs="?",
        type=Path,
        help="capture directory; defaults to the collection LATEST pointer",
    )
    _add_config_argument(reader)
    reader.add_argument("--json", action="store_true", help="emit machine-readable result")

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
        except ConfigError as exc:
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
                result = execute_capture(
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
            print(json.dumps(result.to_dict(), indent=2))
        else:
            print(f"Capture: {result.capture_directory}")
            print(f"Status: {result.status}")
        return 0 if result.status in {"complete", "complete_with_warnings"} else 1
    if args.command == "verify":
        result = verify_capture(args.path)
        if args.json:
            print(json.dumps(result.to_dict(), indent=2))
        else:
            marker = "ok" if result.ok else "FAIL"
            print(f"[{marker}] {result.capture_directory}: {result.checked_objects} object(s)")
            for error in result.errors:
                print(f"  {error}")
        return 0 if result.ok else 1
    if args.command == "analyze" and args.analyze_command == "preservation":
        try:
            config = load_config(args.config)
            result = analyze_preservation(
                config,
                args.collection_id,
                args.capture_paths or None,
            )
        except (ConfigError, AnalysisError) as exc:
            print(f"analysis error: {exc}", file=sys.stderr)
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
    if args.command == "derive" and args.derive_command == "reader":
        try:
            config = load_config(args.config)
            result = build_static_reader(
                config,
                args.collection_id,
                args.capture_path,
            )
        except (ConfigError, AnalysisError) as exc:
            print(f"reader error: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(result.to_dict(), indent=2))
        else:
            print(f"Capture: {result.capture_directory}")
            print(f"Reader: {result.index_path}")
            if result.current_index_path is not None:
                print(f"Current reader: {result.current_index_path}")
            print(f"Status: {result.metadata['status']}")
            for warning in result.metadata.get("warnings", []):
                print(f"  warning: {warning}")
        return 0 if result.metadata["status"] in {"complete", "complete_with_warnings"} else 1
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
    raise AssertionError(f"unhandled command: {args.command}")


def _add_config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=Path("collections.toml"),
        help="TOML configuration file (default: collections.toml)",
    )


def _collection_summary(collection: CollectionConfig) -> dict[str, object]:
    return {
        "id": collection.id,
        "title": collection.title,
        "enabled": collection.enabled,
        "source_count": len(collection.sources),
        "recipe_path": str(collection.recipe_path) if collection.recipe_path else None,
    }


def _collection_detail(collection: CollectionConfig) -> dict[str, object]:
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
