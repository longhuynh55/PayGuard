"""Single-entry P5.1 benchmark and immutable lock commands."""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from payguard.harness import BENCHMARK_COST_CAP_USD, HarnessError, run_benchmark
from payguard.identity import (
    LOCK_PROFILE_CHOICES,
    POLICY_PROVIDERS,
    RunIdentityError,
    create_lock,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m payguard")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser(
        "run",
        help="run the smoke pipeline and one selected policy on an isolated DB copy",
    )
    run_parser.add_argument("--profile", choices=("smoke",), default="smoke")
    run_parser.add_argument("--policy", choices=tuple(POLICY_PROVIDERS), required=True)
    run_parser.add_argument("--out", required=True, help="benchmark root; reuse to resume/add policies")
    run_parser.add_argument(
        "--analysis-role",
        choices=("development", "calibration", "test"),
        default="development",
        help="episode role included in this policy's evaluator report",
    )

    lock_parser = subparsers.add_parser("lock", help="lock hashes before calibration or test analysis")
    lock_commands = lock_parser.add_subparsers(dest="lock_kind", required=True)
    prompt_lock = lock_commands.add_parser("prompt-rule", help="lock prompt and rule inputs")
    prompt_lock.add_argument("--out", required=True, help="benchmark root")
    prompt_lock.add_argument("--profile", choices=tuple(sorted(LOCK_PROFILE_CHOICES)), default="smoke")
    policy_lock = lock_commands.add_parser("policy", help="lock one named policy")
    policy_lock.add_argument("--out", required=True, help="benchmark root")
    policy_lock.add_argument("--policy", choices=tuple(POLICY_PROVIDERS), required=True)
    policy_lock.add_argument("--profile", choices=tuple(sorted(LOCK_PROFILE_CHOICES)), default="smoke")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "lock":
            lock_path = create_lock(
                args.out,
                args.lock_kind,
                policy_name=getattr(args, "policy", None),
                profile=args.profile,
            )
            print(f"lock_kind={args.lock_kind}")
            print(f"profile={args.profile}")
            if getattr(args, "policy", None):
                print(f"policy={args.policy}")
            print(f"lock={lock_path}")
            return 0

        report = run_benchmark(
            profile=args.profile,
            policy_name=args.policy,
            output_dir=args.out,
            analysis_role=args.analysis_role,
        )
    except (HarnessError, RunIdentityError, OSError, ValueError) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print("PayGuard benchmark run completed")
    print(f"policy={report['policy_name']}")
    print(f"analysis_role={report['evaluation']['episode_role']}")
    print(f"base_fingerprint={report['base_fingerprint']}")
    print(f"run_fingerprint={report['run_fingerprint']}")
    print(f"decisions={report['evaluation']['decision_counts']['total']}")
    print(f"evaluation_report={report['evaluation']['report_path']}")
    print(f"benchmark_cost_cap_usd={BENCHMARK_COST_CAP_USD:.2f}")
    print(f"cost_ledger={report['cost_ledger']['path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

