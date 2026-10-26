"""Minimal command line interface for P3 monitoring metrics."""

from __future__ import annotations

import argparse
from pathlib import Path
import sqlite3
import sys
from typing import Sequence

from monitoring.metrics import compute_daily_metrics, write_metrics_csv


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m monitoring")
    subparsers = parser.add_subparsers(dest="command", required=True)
    metrics_parser = subparsers.add_parser("metrics", help="compute daily round-one metrics")
    metrics_parser.add_argument("--operational-db", required=True)
    metrics_parser.add_argument("--simulator-config", default="configs/simulator.yaml")
    metrics_parser.add_argument("--episode-config", default="configs/episodes.yaml")
    metrics_parser.add_argument("--monitoring-config", default="configs/monitoring.yaml")
    metrics_parser.add_argument("--episode-id", action="append")
    metrics_parser.add_argument("--output", help="write CSV here; default is CSV on stdout")
    return parser


def _read_only_connection(path: str | Path) -> sqlite3.Connection:
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise ValueError(f"operational database does not exist: {resolved}")
    connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command != "metrics":
        raise ValueError(f"unsupported monitoring command: {args.command}")
    with _read_only_connection(args.operational_db) as operational:
        rows = compute_daily_metrics(
            operational,
            simulator_config_path=args.simulator_config,
            episode_config_path=args.episode_config,
            monitoring_config_path=args.monitoring_config,
            episode_ids=tuple(args.episode_id) if args.episode_id else None,
        )
    if args.output:
        write_metrics_csv(rows, args.output)
        print(f"metric_count={len(rows)} output={Path(args.output)}", file=sys.stderr)
    else:
        write_metrics_csv(rows, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
