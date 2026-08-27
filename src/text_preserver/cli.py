"""Command-line interface for text-preserver."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from text_preserver import __version__
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
    raise AssertionError(f"unhandled command: {args.command}")
