"""Command line interface for private-side triage decision evaluation."""

from __future__ import annotations

import argparse
from pathlib import Path
import sqlite3
import sys
from typing import Sequence

from evaluation.decisions import evaluate_operational_decisions
from payguard.identity import (
    assert_role_access,
    find_harness_root,
    policy_name_for_run,
    policy_identity,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m evaluation")
    subparsers = parser.add_subparsers(dest="command", required=True)
    decisions_parser = subparsers.add_parser(
        "decisions", help="evaluate persisted P4 decisions against private truth"
    )
    decisions_parser.add_argument("--run", required=True, help="generated run directory")
    decisions_parser.add_argument(
        "--policy-version",
        help="policy_version of the pair to report; needed if agent decisions span several pairs",
    )
    decisions_parser.add_argument(
        "--model-slug",
        help="model_slug of the pair to report; needed if agent decisions span several pairs",
    )
    decisions_parser.add_argument(
        "--episode-role",
        choices=("development", "calibration", "test"),
        action="append",
        help="evaluate only this role; repeat to select multiple roles",
    )
    decisions_parser.add_argument("--out", help="optional per-run report path")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command != "decisions":
        raise ValueError(f"unsupported evaluation command: {args.command}")
    run_dir = Path(args.run).expanduser().resolve()
    try:
        operational_path = run_dir / "operational" / "observations.sqlite"
        harness_root = find_harness_root(run_dir)
        private_path = (
            harness_root / "base" / "private" / "evaluator.sqlite"
            if harness_root is not None
            else run_dir / "private" / "evaluator.sqlite"
        )
        selected_episode_ids = None
        report_path = Path(args.out).expanduser().resolve() if args.out else None
        policy_name = policy_name_for_run(run_dir) if harness_root is not None else None
        if harness_root is not None and policy_name is None:
            raise ValueError("P5 evaluator requires a policy run directory, not BASE")
        policy_version = args.policy_version
        model_slug = args.model_slug
        if harness_root is not None:
            connection = sqlite3.connect(
                f"{operational_path.resolve().as_uri()}?mode=ro", uri=True
            )
            try:
                role_rows = connection.execute(
                    "SELECT episode_id, role FROM episodes WHERE episode_id <> 'base' "
                    "ORDER BY role, episode_id"
                ).fetchall()
            finally:
                connection.close()
            selected_roles = set(args.episode_role or (row[1] for row in role_rows))
            role_episode_ids: dict[str, list[str]] = {}
            for episode_id, role in role_rows:
                if role in selected_roles:
                    role_episode_ids.setdefault(str(role), []).append(str(episode_id))
            selected_episode_ids = tuple(
                episode_id
                for role in sorted(role_episode_ids)
                for episode_id in role_episode_ids[role]
            )
            assert_role_access(
                harness_root,
                operation="evaluation.decisions.cli",
                role_episode_ids=role_episode_ids,
                policy_name=policy_name,
            )
            if policy_name:
                locked_policy = policy_identity(policy_name)
                if policy_version is None:
                    policy_version = str(locked_policy["policy_version"])
                if model_slug is None:
                    model_slug = locked_policy["model_slug"]
            if report_path is None:
                report_path = (
                    harness_root
                    / "base"
                    / "private"
                    / "reports"
                    / "p5_1"
                    / (policy_name or "unselected")
                    / "decisions_cli.json"
                )
            private_reports = (
                harness_root / "base" / "private" / "reports"
            ).resolve()
            try:
                report_path.resolve().relative_to(private_reports)
            except ValueError as exc:
                raise ValueError(
                    "P5 evaluator reports must stay under the shared private reports directory"
                ) from exc
        result = evaluate_operational_decisions(
            operational_path,
            private_db_path=private_path,
            policy_version=policy_version,
            model_slug=model_slug,
            episode_ids=selected_episode_ids,
            report_path=report_path,
        )
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    selection = result.report["selection"]
    print("P4.5 decision evaluation completed")
    print(f"decision_count={result.report['decision_counts']['total']}")
    print(f"policy_version={selection['policy_version'] or 'NULL'}")
    print(f"model_slug={selection['model_slug'] or 'NULL'}")
    print(f"report={result.report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
