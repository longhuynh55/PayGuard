"""CLI for generating and validating locked P1 smoke and pilot runs."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from model.data import load_model_config
from simulator.config import (
    load_episode_config,
    load_simulator_config,
    validate_episode_duration_match,
)
from simulator.episodes import generate_episodes
from simulator.reports import build_smoke_reports, resolved_support_config
from simulator.reproducibility import (
    RUN_FINGERPRINT_METADATA_KEY,
    canonical_run_fingerprint,
    record_canonical_fingerprint,
)
from simulator.storage import create_export_stores, insert_private_record, utc_timestamp
from simulator.validate import RUN_METADATA_KEY, validate_run


CODE_VERSION = "p1.8-smoke-pilot-round2-v1"
SCHEMA_VERSION = 1


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _git_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = result.stdout.strip().lower()
    return commit if commit and all(char in "0123456789abcdef" for char in commit) else None


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _generate(args: argparse.Namespace) -> int:
    output = Path(args.out).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError(f"output directory already contains files: {output}")

    simulator_config = load_simulator_config(args.config)
    episode_config = load_episode_config(args.episodes_config)
    validate_episode_duration_match(simulator_config, episode_config)
    support_config = resolved_support_config(
        load_model_config(args.model_config),
        args.model_config,
        args.model_doc,
    )
    resolved_config = {
        "simulator": simulator_config.model_dump(mode="json"),
        "episodes": episode_config.model_dump(mode="json"),
        "model_support": support_config,
    }
    config_fingerprint = _canonical_hash(resolved_config)
    started_at = datetime.now(UTC)
    started_monotonic = time.monotonic()
    with create_export_stores(output) as stores:
        generate_episodes(
            stores.operational,
            stores.private,
            simulator_config,
            episode_config,
            profile=args.profile,
        )
        record_canonical_fingerprint(stores.operational, stores.private)
        fingerprint = canonical_run_fingerprint(stores.operational)
        insert_private_record(
            stores.private,
            "run_metadata",
            {
                "key": RUN_FINGERPRINT_METADATA_KEY,
                "value_json": {
                    "algorithm": "sha256",
                    "canonicalization": "all operational tables in sorted table and primary-key order; sorted JSON keys",
                    "tables": sorted(
                        row[0]
                        for row in stores.operational.execute(
                            "SELECT name FROM sqlite_master WHERE type='table' "
                            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                        )
                    ),
                    "fingerprint": fingerprint,
                },
            },
        )
        if args.profile == "smoke":
            public_report, private_report = build_smoke_reports(
                stores.operational,
                stores.private,
                simulator_config,
                episode_config,
                model_config_path=args.model_config,
                model_doc_path=args.model_doc,
            )
            _write_json(output / "reports" / "smoke.json", public_report)
            _write_json(output / "private" / "reports" / "smoke_truth.json", private_report)

        completed_at = datetime.now(UTC)
        runtime = {
            "started_at": utc_timestamp(started_at),
            "completed_at": utc_timestamp(completed_at),
            "elapsed_seconds": round(time.monotonic() - started_monotonic, 6),
        }
        metadata = {
            "artifact_version": SCHEMA_VERSION,
            "profile": args.profile,
            "resolved_config": resolved_config,
            "seed": {
                "simulator": simulator_config.seed.value,
                "episodes": episode_config.seed.value,
            },
            "versions": {
                "schema": SCHEMA_VERSION,
                "code": CODE_VERSION,
                "git_commit": _git_commit(),
            },
            "config_fingerprint": config_fingerprint,
            "canonical_content_fingerprint": fingerprint,
            "runtime": runtime,
            "validation": None,
        }
        insert_private_record(
            stores.private,
            "run_metadata",
            {"key": RUN_METADATA_KEY, "value_json": metadata},
        )

    validation = validate_run(output)
    print(f"Generated {args.profile} run: {output}")
    print(f"Canonical operational fingerprint: {fingerprint}")
    print(f"Validation: {validation['status'].upper()}")
    _print_validation(validation)
    return 0 if validation["status"] == "passed" else 1


def _print_validation(result: dict[str, Any]) -> None:
    for check in result["checks"]:
        marker = "PASS" if check["passed"] else "FAIL"
        print(f"{marker} {check['name']}: {check['detail']}")
    print(
        f"Validation summary: {result['passed_checks']} passed, "
        f"{result['failed_checks']} failed"
    )


def _validate(args: argparse.Namespace) -> int:
    result = validate_run(args.run)
    _print_validation(result)
    return 0 if result["status"] == "passed" else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m simulator")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate_parser = subparsers.add_parser(
        "generate", help="generate a locked P1 smoke or pilot profile"
    )
    generate_parser.add_argument("--profile", choices=("smoke", "pilot"), default="smoke")
    generate_parser.add_argument("--out", required=True, help="new run directory")
    generate_parser.add_argument("--config", default="configs/simulator.yaml")
    generate_parser.add_argument("--episodes-config", default="configs/episodes.yaml")
    generate_parser.add_argument("--model-config", default="configs/model.yaml")
    generate_parser.add_argument("--model-doc", default="docs/model.md")
    generate_parser.set_defaults(handler=_generate)

    validate_parser = subparsers.add_parser("validate", help="validate a generated run")
    validate_parser.add_argument("--run", required=True, help="generated run directory")
    validate_parser.set_defaults(handler=_validate)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.handler(args)
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

