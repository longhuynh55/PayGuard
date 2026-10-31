"""CLI for the P4 operational monitoring-to-triage smoke loop."""

from __future__ import annotations

import argparse
from collections import defaultdict
import sqlite3
import sys
from pathlib import Path
from typing import Sequence

from triage.agent import build_agent_provider
from triage.cost_ledger import DurableCostLedger
from triage.measure import measure_triage
from triage.runner import run_triage
from payguard.identity import (
    RunIdentityError,
    assert_role_access,
    benchmark_state_directory,
    find_harness_root,
    policy_name_for_run,
    policy_identity,
    repository_root,
    record_role_touches,
)
from payguard.harness import BENCHMARK_COST_CAP_USD, verify_harness_policy_run


CONFIG_ROOT = repository_root() / "configs"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m triage")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser(
        "run", help="run daily monitoring and operational triage for a smoke run"
    )
    run_parser.add_argument("--run", required=True, help="generated run directory")
    run_parser.add_argument("--simulator-config", default=str(CONFIG_ROOT / "simulator.yaml"))
    run_parser.add_argument("--triage-config", default=str(CONFIG_ROOT / "triage.yaml"))
    run_parser.add_argument(
        "--agent-provider",
        choices=("unconfigured", "fake", "openrouter"),
        default="unconfigured",
        help="agent provider (OpenRouter is only used when selected explicitly)",
    )
    run_parser.add_argument("--llm-config", default=str(CONFIG_ROOT / "llm.yaml"))

    measure_parser = subparsers.add_parser(
        "measure", help="measure one-step agent tokens on a fixed incident sample"
    )
    measure_parser.add_argument("--run", required=True, help="generated run directory")
    measure_parser.add_argument(
        "--agent-provider", choices=("fake", "openrouter"), required=True
    )
    measure_parser.add_argument("--incidents", type=int, default=3)
    measure_parser.add_argument("--out", required=True, help="public-safe JSON report path")
    measure_parser.add_argument("--simulator-config", default=str(CONFIG_ROOT / "simulator.yaml"))
    measure_parser.add_argument("--triage-config", default=str(CONFIG_ROOT / "triage.yaml"))
    measure_parser.add_argument("--llm-config", default=str(CONFIG_ROOT / "llm.yaml"))
    return parser


def _build_provider_for_run(
    provider_name: str,
    operational_db: Path,
    run_dir: str | Path,
    *,
    llm_config_path: str | Path,
    simulator_config_path: str | Path,
    triage_config_path: str | Path,
):
    benchmark_root = find_harness_root(run_dir)
    if benchmark_root is None:
        return build_agent_provider(
            provider_name,
            operational_db,
            llm_config_path=llm_config_path,
        ), None, None
    expected_configs = {
        "simulator": CONFIG_ROOT / "simulator.yaml",
        "triage": CONFIG_ROOT / "triage.yaml",
        "llm": CONFIG_ROOT / "llm.yaml",
    }
    actual_configs = {
        "simulator": Path(simulator_config_path).expanduser().resolve(),
        "triage": Path(triage_config_path).expanduser().resolve(),
        "llm": Path(llm_config_path).expanduser().resolve(),
    }
    if actual_configs != expected_configs:
        raise RunIdentityError(
            "P5 harness triage refuses custom config paths not covered by the run manifest"
        )
    policy_name = policy_name_for_run(run_dir)
    if policy_name is None:
        raise ValueError("P5 harness triage requires a policy run directory")
    verify_harness_policy_run(run_dir)
    expected = policy_identity(policy_name)
    if provider_name != expected["provider_name"]:
        raise ValueError(
            f"provider mismatch for P5 policy {policy_name!r}; use "
            f"{expected['provider_name']!r}"
        )
    ledger = DurableCostLedger(
        benchmark_state_directory(benchmark_root) / "cost-ledger.jsonl",
        cap_usd=BENCHMARK_COST_CAP_USD,
    )
    provider = build_agent_provider(
        provider_name,
        operational_db,
        llm_config_path=llm_config_path,
        cost_ledger=ledger,
        ledger_policy=policy_name,
        cost_cap_usd=BENCHMARK_COST_CAP_USD,
    )
    if provider.model_slug != expected["model_slug"]:
        raise ValueError(f"model_slug mismatch for P5 policy {policy_name!r}")
    return provider, benchmark_root, policy_name


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        operational_db = (
            Path(args.run).expanduser().resolve()
            / "operational"
            / "observations.sqlite"
        )
        if args.command == "measure":
            benchmark_root = find_harness_root(args.run)
            policy_name = policy_name_for_run(args.run) if benchmark_root else None

            def check_measure_role(role: str, episode_ids: tuple[str, ...]) -> None:
                if benchmark_root is not None:
                    assert_role_access(
                        benchmark_root,
                        operation="triage.measure",
                        role_episode_ids={role: episode_ids},
                        policy_name=policy_name,
                    )

            provider, _, _ = _build_provider_for_run(
                args.agent_provider,
                operational_db,
                args.run,
                llm_config_path=args.llm_config,
                simulator_config_path=args.simulator_config,
                triage_config_path=args.triage_config,
            )
            report = measure_triage(
                args.run,
                agent_provider=provider,
                provider_name=args.agent_provider,
                incidents=args.incidents,
                out=args.out,
                simulator_config_path=args.simulator_config,
                triage_config_path=args.triage_config,
                role_access_callback=check_measure_role if benchmark_root else None,
            )
            print("P4.6 token measurement completed")
            print(f"status={report['status']}")
            print(f"incidents={len(report['incident_totals'])}")
            print(f"calls={report['totals']['call_count']}")
            print(f"total_tokens={report['totals']['total_tokens']}")
            print(f"cost_usd={report['totals']['cost_usd']}")
            if report.get("error_status"):
                print(f"error_status={report['error_status']}")
            print(f"report={Path(args.out).expanduser().resolve()}")
            return 0 if report["status"] == "complete" else 2

        if args.command != "run":
            raise ValueError(f"unsupported triage command: {args.command}")
        agent_provider, _, _ = _build_provider_for_run(
            args.agent_provider,
            operational_db,
            args.run,
            llm_config_path=args.llm_config,
            simulator_config_path=args.simulator_config,
            triage_config_path=args.triage_config,
        )
        result = run_triage(
            args.run,
            simulator_config_path=args.simulator_config,
            triage_config_path=args.triage_config,
            agent_provider=agent_provider,
        )
        benchmark_root = find_harness_root(args.run)
        if benchmark_root is not None:
            connection = sqlite3.connect(
                f"{operational_db.resolve().as_uri()}?mode=ro", uri=True
            )
            try:
                rows = connection.execute(
                    "SELECT episode_id, role FROM episodes WHERE episode_id <> 'base' "
                    "ORDER BY role, episode_id"
                ).fetchall()
            finally:
                connection.close()
            roles: dict[str, list[str]] = defaultdict(list)
            for episode_id, role in rows:
                roles[str(role)].append(str(episode_id))
            record_role_touches(
                benchmark_root,
                operation="triage.run",
                role_episode_ids=roles,
                policy_name=policy_name_for_run(args.run),
                usage="operational_execution",
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
