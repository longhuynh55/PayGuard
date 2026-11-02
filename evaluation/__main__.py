"""Command line interface for private-side triage decision evaluation."""

from __future__ import annotations

import argparse
from pathlib import Path
import sqlite3
import sys
from typing import Sequence

from evaluation.decisions import evaluate_operational_decisions
from evaluation.matcher import assert_operational_roles_match_private_manifest
from evaluation.support import run_development_support
from payguard.identity import (
    assert_role_access,
    find_harness_root,
    policy_name_for_run,
    policy_identity,
)
from payguard.run_identity import verify_harness_policy_run


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
    support_parser = subparsers.add_parser(
        "support",
        help="measure P5.0b support using development episodes only",
    )
    support_parser.add_argument(
        "--run",
        required=True,
        help="P5.1 policy run directory inside a benchmark root",
    )
    support_parser.add_argument(
        "--out",
        help="optional report path under the benchmark's private reports directory",
    )
    return parser


def _standalone_policy_name(
    operational_path: Path,
    *,
    policy_version: str | None,
    model_slug: str | None,
) -> str:
    connection = sqlite3.connect(
        f"{operational_path.resolve().as_uri()}?mode=ro", uri=True
    )
    try:
        rows = connection.execute(
            "SELECT DISTINCT policy_version, model_slug FROM decisions "
            "WHERE source='agent' ORDER BY policy_version, model_slug"
        ).fetchall()
        if not rows:
            rows = connection.execute(
                "SELECT DISTINCT policy_version, NULL AS model_slug FROM decisions "
                "WHERE source='rule' ORDER BY policy_version"
            ).fetchall()
    finally:
        connection.close()
    pairs = {
        (str(row[0]), str(row[1]) if row[1] is not None else None)
        for row in rows
    }
    if policy_version is not None:
        pairs = {pair for pair in pairs if pair[0] == policy_version}
    if model_slug is not None:
        pairs = {pair for pair in pairs if pair[1] == model_slug}
    if len(pairs) != 1:
        raise ValueError(
            "held-out standalone evaluation requires one selected supported policy pair; "
            "pass selectors or use the P5.1 harness"
        )
    selected_version, selected_slug = next(iter(pairs))
    if selected_slug is None:
        name = "rule_human"
    elif selected_slug == "fake/payguard-p4-round1":
        name = "rule_agent_fake"
    elif selected_slug == policy_identity("rule_agent_openrouter_k1")["model_slug"]:
        name = "rule_agent_openrouter_k1"
    else:
        raise ValueError(
            f"held-out standalone evaluation cannot lock unsupported model {selected_slug!r}"
        )
    expected = policy_identity(name)
    if selected_version != expected["policy_version"] or selected_slug != expected["model_slug"]:
        raise ValueError(
            "held-out standalone evaluation policy version/model do not match the "
            "current named policy lock identity"
        )
    return name


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "support":
        try:
            result = run_development_support(run_dir=args.run, report_path=args.out)
        except Exception as exc:
            print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        print("P5.0b development support report completed")
        print(f"episode_role={result.report['episode_role']}")
        print(f"episode_count={len(result.report['episode_ids'])}")
        print(f"incident_count={result.report['incident_counts']['evaluated']}")
        print(f"agent_incidents={result.report['agent_support']['incidents_reaching_agent']}")
        print(f"report={result.report_path}")
        return 0
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
        report_path = Path(args.out).expanduser().resolve() if args.out else None
        policy_name = policy_name_for_run(run_dir) if harness_root is not None else None
        if harness_root is not None and policy_name is None:
            raise ValueError("P5 evaluator requires a policy run directory, not BASE")
        policy_version = args.policy_version
        model_slug = args.model_slug
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
        if args.episode_role:
            selected_roles = set(args.episode_role)
        elif harness_root is not None:
            selected_roles = {str(row[1]) for row in role_rows}
        else:
            selected_roles = {"development"}
        role_episode_ids: dict[str, list[str]] = {}
        for episode_id, role in role_rows:
            if role in selected_roles:
                role_episode_ids.setdefault(str(role), []).append(str(episode_id))
        if selected_roles != set(role_episode_ids):
            raise ValueError(
                f"selected episode roles have no episodes: "
                f"{sorted(selected_roles - set(role_episode_ids))}"
            )
        selected_episode_ids = tuple(
            episode_id
            for role in sorted(role_episode_ids)
            for episode_id in role_episode_ids[role]
        )
        if harness_root is not None:
            verify_harness_policy_run(
                run_dir, required_roles=tuple(sorted(role_episode_ids))
            )
        elif any(role != "development" for role in selected_roles):
            policy_name = _standalone_policy_name(
                operational_path,
                policy_version=policy_version,
                model_slug=model_slug,
            )
        assert_operational_roles_match_private_manifest(
            private_path,
            {str(episode_id): str(role) for episode_id, role in role_rows},
        )
        if harness_root is not None or any(
            role != "development" for role in selected_roles
        ):
            assert_role_access(
                harness_root or run_dir,
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
            if any(role != "development" for role in selected_roles) and (
                policy_version != locked_policy["policy_version"]
                or model_slug != locked_policy["model_slug"]
            ):
                raise ValueError("selected decision pair does not match its held-out lock identity")
        if harness_root is not None:
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
