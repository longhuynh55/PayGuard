"""Command line interface for the P2 train, score, and evaluate flow."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
from typing import Sequence

from model.evaluate import (
    evaluate_model_scores,
    score_monitoring_episodes,
    train_operational_model,
)
from model.train import artifact_directory_for_run


def _connection(path: str | Path, *, read_only: bool) -> sqlite3.Connection:
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise ValueError(f"database does not exist: {resolved}")
    uri = f"{resolved.as_uri()}?mode={'ro' if read_only else 'rw'}"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m model")
    subparsers = parser.add_subparsers(dest="command", required=True)

    train_parser = subparsers.add_parser("train", help="run P2.1–P2.4 and save model artifacts")
    train_parser.add_argument("--run-id", required=True)
    train_parser.add_argument("--operational-db", required=True)
    train_parser.add_argument("--episode-id")
    train_parser.add_argument("--data-root", default="data")
    train_parser.add_argument("--simulator-config", default="configs/simulator.yaml")
    train_parser.add_argument("--model-config", default="configs/model.yaml")

    score_parser = subparsers.add_parser("score", help="score all registered monitoring episodes")
    score_parser.add_argument("--run-id", required=True)
    score_parser.add_argument("--operational-db", required=True)
    score_parser.add_argument("--data-root", default="data")
    score_parser.add_argument("--simulator-config", default="configs/simulator.yaml")
    score_parser.add_argument("--episode-config", default="configs/episodes.yaml")

    evaluate_parser = subparsers.add_parser(
        "evaluate", help="report smoke PR-AUC and Mondrian coverage using private truth"
    )
    evaluate_parser.add_argument("--run-id", required=True)
    evaluate_parser.add_argument("--operational-db", required=True)
    evaluate_parser.add_argument("--private-db", required=True)
    evaluate_parser.add_argument("--data-root", default="data")
    evaluate_parser.add_argument("--simulator-config", default="configs/simulator.yaml")
    evaluate_parser.add_argument("--episode-config", default="configs/episodes.yaml")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "train":
        with _connection(args.operational_db, read_only=True) as operational:
            artifact_dir, audit = train_operational_model(
                operational,
                run_id=args.run_id,
                data_root=args.data_root,
                simulator_config_path=args.simulator_config,
                model_config_path=args.model_config,
                episode_id=args.episode_id,
            )
        print(audit)
        print(f"model_artifacts={artifact_dir}")
        return 0

    artifact_dir = artifact_directory_for_run(args.run_id, data_root=args.data_root)
    if args.command == "score":
        with _connection(args.operational_db, read_only=False) as operational:
            rows = score_monitoring_episodes(
                operational,
                model_directory=artifact_dir,
                simulator_config_path=args.simulator_config,
                episode_config_path=args.episode_config,
            )
            operational.commit()
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["episode_id"]] = counts.get(row["episode_id"], 0) + 1
        print(json.dumps({"score_count": len(rows), "episode_counts": counts}, sort_keys=True))
        return 0

    with (
        _connection(args.operational_db, read_only=True) as operational,
        _connection(args.private_db, read_only=True) as private,
    ):
        report = evaluate_model_scores(
            operational,
            private,
            model_directory=artifact_dir,
            simulator_config_path=args.simulator_config,
            episode_config_path=args.episode_config,
        )
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
