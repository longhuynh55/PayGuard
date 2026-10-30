"""Command line interface for private-side triage decision evaluation."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Sequence

from evaluation.decisions import evaluate_operational_decisions


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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command != "decisions":
        raise ValueError(f"unsupported evaluation command: {args.command}")
    run_dir = Path(args.run).expanduser().resolve()
    try:
        result = evaluate_operational_decisions(
            run_dir / "operational" / "observations.sqlite",
            private_db_path=run_dir / "private" / "evaluator.sqlite",
            policy_version=args.policy_version,
            model_slug=args.model_slug,
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
