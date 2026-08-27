"""Command-line interface for text-preserver."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from text_preserver import __version__
from text_preserver.capture import (
    CaptureExecutionError,
    CapturePlanError,
    execute_capture,
    plan_capture,
)
from text_preserver.config import ConfigError, load_config
from text_preserver.doctor import inspect_environment


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
    raise AssertionError(f"unhandled command: {args.command}")
