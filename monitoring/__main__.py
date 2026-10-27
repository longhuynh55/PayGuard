"""Minimal command line interface for P3 monitoring metrics and signals."""

from __future__ import annotations

import argparse
import csv
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import sys
from typing import Iterator, Sequence

from monitoring.incidents import (
    InMemoryDecisionSource,
    compute_incidents,
    write_incidents_csv,
)
from monitoring.metrics import compute_daily_metrics, write_metrics_csv
from monitoring.signals import ALERT_COLUMNS, compute_alerts, write_alerts_csv
from monitoring.store import persist_monitoring_rows


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m monitoring")
    subparsers = parser.add_subparsers(dest="command", required=True)
    metrics_parser = subparsers.add_parser("metrics", help="compute daily round-one metrics")
    metrics_parser.add_argument("--operational-db", required=True)
    metrics_parser.add_argument("--simulator-config", default="configs/simulator.yaml")
    metrics_parser.add_argument("--monitoring-config", default="configs/monitoring.yaml")
    metrics_parser.add_argument("--episode-id", action="append")
    metrics_parser.add_argument("--output", help="write CSV here; default is CSV on stdout")

    signals_parser = subparsers.add_parser(
        "signals", help="compute round-one PSI alerts for one episode snapshot"
    )
    signals_parser.add_argument("--operational-db", required=True)
    signals_parser.add_argument("--simulator-config", default="configs/simulator.yaml")
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

    incidents_parser = subparsers.add_parser(
        "incidents", help="group one episode's P3.2 alert CSV into incidents"
    )
    incidents_parser.add_argument("--operational-db", required=True)
    incidents_parser.add_argument("--episode-id", required=True)
    incidents_parser.add_argument("--alerts-csv", required=True)
    incidents_parser.add_argument(
        "--decisions-json",
        help="optional JSON array of decisions; omit before P4 is available",
    )
    incidents_parser.add_argument(
        "--output", help="write incident CSV here; default is CSV on stdout"
    )
    return parser


@contextmanager
def _operational_connection(path: str | Path) -> Iterator[sqlite3.Connection]:
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise ValueError(f"operational database does not exist: {resolved}")
    connection = sqlite3.connect(resolved)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _read_alerts_csv(path: str | Path) -> list[dict[str, object]]:
    with Path(path).open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != ALERT_COLUMNS:
            raise ValueError("alerts CSV header does not match the P3.2 output schema")
        rows: list[dict[str, object]] = []
        for row in reader:
            parsed: dict[str, object] = dict(row)
            for field in ("value", "threshold"):
                parsed[field] = float(parsed[field])
            rows.append(parsed)
        return rows


def _read_decisions_json(path: str | Path | None) -> tuple[dict[str, object], ...]:
    if path is None:
        return ()
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError("decisions JSON must be an array of decision objects")
    return tuple(value)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "incidents":
        alerts = _read_alerts_csv(args.alerts_csv)
        decision_source = InMemoryDecisionSource(_read_decisions_json(args.decisions_json))
        with _operational_connection(args.operational_db) as operational:
            persist_monitoring_rows(operational, "alerts", alerts)
            result = compute_incidents(
                alerts,
                episode_id=args.episode_id,
                decision_source=decision_source,
            )
            persist_monitoring_rows(operational, "incidents", result.incidents)
        if args.output:
            write_incidents_csv(result.incidents, args.output)
            print(
                f"incident_count={result.report['incident_count']} "
                f"snapshot_count={result.report['snapshot_count']} output={Path(args.output)}",
                file=sys.stderr,
            )
        else:
            write_incidents_csv(result.incidents, sys.stdout)
        return 0

    if args.command not in {"metrics", "signals"}:
        raise ValueError(f"unsupported monitoring command: {args.command}")
    with _operational_connection(args.operational_db) as operational:
        rows = compute_daily_metrics(
            operational,
            simulator_config_path=args.simulator_config,
            monitoring_config_path=args.monitoring_config,
            episode_ids=(args.episode_id,)
            if args.command == "signals"
            else tuple(args.episode_id) if args.episode_id else None,
        )
        persist_monitoring_rows(operational, "daily_metrics", rows)
    if args.command == "signals":
        result = compute_alerts(
            rows,
            episode_id=args.episode_id,
            as_of_time=args.as_of_time,
            monitoring_config_path=args.monitoring_config,
        )
        with _operational_connection(args.operational_db) as operational:
            persist_monitoring_rows(operational, "alerts", result.alerts)
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
