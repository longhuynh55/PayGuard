"""CLI for the P4 operational monitoring-to-triage smoke loop."""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from triage.runner import run_triage


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m triage")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser(
        "run", help="run daily monitoring and operational triage for a smoke run"
    )
    run_parser.add_argument("--run", required=True, help="generated run directory")
    run_parser.add_argument("--simulator-config", default="configs/simulator.yaml")
    run_parser.add_argument("--triage-config", default="configs/triage.yaml")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command != "run":
        raise ValueError(f"unsupported triage command: {args.command}")
    try:
        result = run_triage(
            args.run,
            simulator_config_path=args.simulator_config,
            triage_config_path=args.triage_config,
        )
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print("P4 triage completed")
    for key, value in result.items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
