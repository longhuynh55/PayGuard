"""Minimal command line interface for P3 monitoring metrics and signals."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
from typing import Sequence

from monitoring.metrics import compute_daily_metrics, write_metrics_csv
from monitoring.signals import compute_alerts, write_alerts_csv


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

    signals_parser = subparsers.add_parser(
        "signals", help="compute round-one PSI alerts for one episode snapshot"
    )
    signals_parser.add_argument("--operational-db", required=True)
    signals_parser.add_argument("--simulator-config", default="configs/simulator.yaml")
    signals_parser.add_argument("--episode-config", default="configs/episodes.yaml")
    signals_parser.add_argument("--monitoring-config", default="configs/monitoring.yaml")
    signals_parser.add_argument("--episode-id", required=True)
    signals_parser.add_argument(
        "--as-of-time",
        required=True,
        help="UTC snapshot cutoff, for example 2026-02-10T16:59:59.999999Z",
    )
    signals_parser.add_argument("--output", help="write alert CSV here; default is CSV on stdout")
    signals_parser.add_argument(
        "--report-output", help="write JSON suppression report here; default is JSON on stderr"
    )
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
    if args.command not in {"metrics", "signals"}:
        raise ValueError(f"unsupported monitoring command: {args.command}")
    with _read_only_connection(args.operational_db) as operational:
        rows = compute_daily_metrics(
            operational,
            simulator_config_path=args.simulator_config,
            episode_config_path=args.episode_config,
            monitoring_config_path=args.monitoring_config,
            episode_ids=(args.episode_id,)
            if args.command == "signals"
            else tuple(args.episode_id) if args.episode_id else None,
        )
    if args.command == "signals":
        result = compute_alerts(
            rows,
            episode_id=args.episode_id,
            as_of_time=args.as_of_time,
            monitoring_config_path=args.monitoring_config,
        )
        if args.output:
            write_alerts_csv(result.alerts, args.output)
            print(f"alert_count={len(result.alerts)} output={Path(args.output)}", file=sys.stderr)
        else:
            write_alerts_csv(result.alerts, sys.stdout)
        report_contents = json.dumps(
            result.report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
        ) + "\n"
        if args.report_output:
            report_path = Path(args.report_output)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(report_contents, encoding="utf-8", newline="")
            print(f"report_output={report_path}", file=sys.stderr)
        else:
            print(report_contents, file=sys.stderr, end="")
        return 0
    if args.output:
        write_metrics_csv(rows, args.output)
        print(f"metric_count={len(rows)} output={Path(args.output)}", file=sys.stderr)
    else:
        write_metrics_csv(rows, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
