"""Command line interface for the P2 train, score, and evaluate flow."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
from typing import Sequence

from model.evaluate import (
    evaluate_model_scores,
    score_monitoring_episodes,
    train_operational_model,
    validate_private_episode_roles,
)
from model.train import artifact_directory_for_run
from payguard.identity import (
    assert_role_access,
    find_harness_root,
    policy_name_for_run,
    repository_root,
)
from payguard.run_identity import (
    verify_harness_model_artifacts,
    verify_harness_policy_run,
)


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

    evaluate_parser = subparsers.add_parser(
        "evaluate", help="report smoke PR-AUC and Mondrian coverage using private truth"
    )
    evaluate_parser.add_argument("--run-id", required=True)
    evaluate_parser.add_argument("--operational-db", required=True)
    evaluate_parser.add_argument("--private-db", required=True)
    evaluate_parser.add_argument("--data-root", default="data")
    evaluate_parser.add_argument("--simulator-config", default="configs/simulator.yaml")
    evaluate_parser.add_argument(
        "--episode-role",
        choices=("development", "calibration", "test"),
        action="append",
        help="evaluate selected roles; standalone runs default to development only",
    )
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
            )
            operational.commit()
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["episode_id"]] = counts.get(row["episode_id"], 0) + 1
        print(json.dumps({"score_count": len(rows), "episode_counts": counts}, sort_keys=True))
        return 0

    operational_path = Path(args.operational_db).expanduser().resolve()
    private_path = Path(args.private_db).expanduser().resolve()
    harness_root = find_harness_root(operational_path)
    roles: dict[str, list[str]] = {}
    with _connection(operational_path, read_only=True) as operational:
        role_rows = operational.execute(
            "SELECT episode_id, role FROM episodes WHERE episode_id <> 'base' "
            "ORDER BY role, episode_id"
        ).fetchall()
    unknown_roles = sorted(
        {str(row[1]) for row in role_rows} - {"development", "calibration", "test"}
    )
    if unknown_roles:
        print(
            f"ERROR: operational episodes have unknown roles: {unknown_roles}",
            file=sys.stderr,
        )
        return 2
    if args.episode_role:
        selected_roles = set(args.episode_role)
    else:
        selected_roles = {"development"}
    held_out_roles = selected_roles & {"calibration", "test"}
    for episode_id, role in role_rows:
        if role in selected_roles:
            roles.setdefault(str(role), []).append(str(episode_id))
    if selected_roles != set(roles):
        print(
            "ERROR: selected episode roles have no episodes: "
            f"{sorted(selected_roles - set(roles))}",
            file=sys.stderr,
        )
        return 2
    if (harness_root is not None or held_out_roles) and (
        Path(args.simulator_config).expanduser().resolve()
        != (repository_root() / "configs" / "simulator.yaml").resolve()
    ):
        print(
            "ERROR: model evaluate refuses custom simulator config paths not covered by "
            "the verified lock/run identity",
            file=sys.stderr,
        )
        return 2
    selected_episode_ids = tuple(
        episode_id for role in sorted(roles) for episode_id in roles[role]
    )
    policy_name = None
    expected_artifact_identity = None
    try:
        if harness_root is not None:
            policy_run = operational_path.parent.parent
            policy_name = policy_name_for_run(policy_run)
            if (
                policy_name is None
                or policy_run.parent != harness_root / "policies"
                or operational_path != policy_run / "operational" / "observations.sqlite"
            ):
                raise ValueError("model evaluate in a harness requires a policy operational copy")
            expected_private = harness_root / "base" / "private" / "evaluator.sqlite"
            if private_path != expected_private.resolve():
                raise ValueError(
                    "model evaluate in a harness must use the frozen BASE evaluator store"
                )
            expected_artifact_identity = verify_harness_model_artifacts(
                policy_run,
                model_directory=artifact_dir,
            )
            verify_harness_policy_run(policy_run, required_roles=tuple(sorted(roles)))
        elif held_out_roles:
            raise ValueError(
                "model evaluate outside a P5.1 harness is limited to development episodes"
            )

        # This reads only the private episode-role manifest. It must pass before
        # role-ledger writes and before the evaluator reads transaction truth.
        with _connection(private_path, read_only=True) as private:
            validate_private_episode_roles(private, roles)

        if harness_root is not None:
            assert_role_access(
                harness_root,
                operation="model.evaluate.cli",
                role_episode_ids=roles,
                policy_name=policy_name,
            )
    except (OSError, ValueError) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    with (
        _connection(operational_path, read_only=True) as operational,
        _connection(private_path, read_only=True) as private,
    ):
        report = evaluate_model_scores(
            operational,
            private,
            model_directory=artifact_dir,
            simulator_config_path=args.simulator_config,
            episode_ids=selected_episode_ids,
            expected_artifact_identity=expected_artifact_identity,
        )
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
