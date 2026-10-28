"""CLI for the P4 operational monitoring-to-triage smoke loop."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from triage.agent import build_agent_provider
from triage.measure import measure_triage
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
    run_parser.add_argument(
        "--agent-provider",
        choices=("unconfigured", "fake", "openrouter"),
        default="unconfigured",
        help="agent provider (OpenRouter is only used when selected explicitly)",
    )
    run_parser.add_argument("--llm-config", default="configs/llm.yaml")

    measure_parser = subparsers.add_parser(
        "measure", help="measure one-step agent tokens on a fixed incident sample"
    )
    measure_parser.add_argument("--run", required=True, help="generated run directory")
    measure_parser.add_argument(
        "--agent-provider", choices=("fake", "openrouter"), required=True
    )
    measure_parser.add_argument("--incidents", type=int, default=3)
    measure_parser.add_argument("--out", required=True, help="public-safe JSON report path")
    measure_parser.add_argument("--simulator-config", default="configs/simulator.yaml")
    measure_parser.add_argument("--triage-config", default="configs/triage.yaml")
    measure_parser.add_argument("--llm-config", default="configs/llm.yaml")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        operational_db = (
            Path(args.run).expanduser().resolve()
            / "operational"
            / "observations.sqlite"
        )
        if args.command == "measure":
            provider = build_agent_provider(
                args.agent_provider,
                operational_db,
                llm_config_path=args.llm_config,
            )
            report = measure_triage(
                args.run,
                agent_provider=provider,
                provider_name=args.agent_provider,
                incidents=args.incidents,
                out=args.out,
                simulator_config_path=args.simulator_config,
                triage_config_path=args.triage_config,
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
        agent_provider = build_agent_provider(
            args.agent_provider,
            operational_db,
            llm_config_path=args.llm_config,
        )
        result = run_triage(
            args.run,
            simulator_config_path=args.simulator_config,
            triage_config_path=args.triage_config,
            agent_provider=agent_provider,
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
