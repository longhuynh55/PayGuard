"""Thin P5.1 orchestration over the existing P1–P4 module APIs."""

from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from typing import Any, Iterator, Mapping

from evaluation.decisions import evaluate_operational_decisions
from model.artifacts import assert_model_artifact_identity, model_artifact_identity
from model.evaluate import score_monitoring_episodes, train_operational_model
from model.train import artifact_directory_for_run
from monitoring.metrics import compute_daily_metrics
from monitoring.signals import compute_alerts
from monitoring.store import persist_monitoring_rows
from simulator.reproducibility import canonical_run_fingerprint
from triage.agent import build_agent_provider
from triage.cost_ledger import DurableCostLedger
from triage.runner import run_triage

from payguard.config import load_benchmark_cost_cap
from payguard.identity import (
    POLICY_PROVIDERS,
    ROLES,
    RunIdentityError,
    assert_harness_state_directory,
    assert_role_access,
    benchmark_state_directory,
    current_hashes,
    current_run_identity,
    identity_hash_mismatches,
    lock_identity_hashes,
    policy_identity,
    record_role_touches,
    run_fingerprint,
)
from payguard.run_identity import verify_harness_policy_run


HARNESS_PROFILES = ("smoke", "pilot")
BENCHMARK_COST_CAP_USD = load_benchmark_cost_cap()
MODEL_RUN_ID = "payguard-p5"
ROOT = Path(__file__).resolve().parents[1]
SIMULATOR_CONFIG = ROOT / "configs/simulator.yaml"
EPISODES_CONFIG = ROOT / "configs/episodes.yaml"
MODEL_CONFIG = ROOT / "configs/model.yaml"
MONITORING_CONFIG = ROOT / "configs/monitoring.yaml"
TRIAGE_CONFIG = ROOT / "configs/triage.yaml"
LLM_CONFIG = ROOT / "configs/llm.yaml"
MODEL_DOC = ROOT / "docs/model.md"


class HarnessError(ValueError):
    """Raised when a benchmark run cannot safely be resumed or completed."""


@contextmanager
def _connection(path: Path, *, read_only: bool) -> Iterator[sqlite3.Connection]:
    if not path.is_file():
        raise HarnessError(f"required SQLite database does not exist: {path}")
    mode = "ro" if read_only else "rw"
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode={mode}", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        if not read_only:
            connection.execute("PRAGMA foreign_keys = ON")
        yield connection
        if not read_only:
            connection.commit()
    except Exception:
        if not read_only:
            connection.rollback()
        raise
    finally:
        connection.close()


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessError(f"run manifest is missing or invalid: {path}") from exc
    if not isinstance(value, dict):
        raise HarnessError(f"run manifest must contain a JSON object: {path}")
    return value


def _ensure_harness_root(
    root: Path,
    identity_hashes: Mapping[str, Any],
    state_dir: Path,
    profile: str,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / "harness-manifest.json"
    if manifest_path.exists():
        manifest = _read_json(manifest_path)
        if manifest.get("schema_version") != 1:
            raise HarnessError(f"existing harness manifest has an unsupported schema: {manifest_path}")
        if manifest.get("profile") != profile:
            raise HarnessError(
                f"existing output directory belongs to profile {manifest.get('profile')!r}, "
                f"not {profile!r}; use a separate benchmark root for each profile"
            )
        assert_harness_state_directory(root, manifest)
        manifest_hashes = manifest.get("hashes")
        differences = (
            identity_hash_mismatches(manifest_hashes, identity_hashes)
            if isinstance(manifest_hashes, dict)
            else ("hashes",)
        )
        if differences:
            raise RunIdentityError(
                "harness resume refused: runtime/config/prompt/sampling hashes differ from "
                f"harness-manifest.json: {', '.join(differences)}"
            )
        return
    existing = {item.name for item in root.iterdir()}
    if existing - {"locks"}:
        raise HarnessError(
            f"output directory is non-empty and has no harness manifest: {root}"
        )
    _write_json(
        manifest_path,
        {
            "schema_version": 1,
            "profile": profile,
            "state_dir": str(state_dir),
            "hashes": dict(identity_hashes),
        },
    )


def _validate_static_identity(
    path: Path,
    expected: Mapping[str, Any],
    *,
    label: str,
    profile: str,
) -> dict[str, Any]:
    manifest = _read_json(path)
    if manifest.get("profile") != profile:
        raise HarnessError(
            f"{label} belongs to profile {manifest.get('profile')!r}, not {profile!r}: {path}"
        )
    manifest_hashes = manifest.get("hashes")
    differences = (
        identity_hash_mismatches(manifest_hashes, expected)
        if isinstance(manifest_hashes, dict)
        else ("hashes",)
    )
    if manifest.get("schema_version") != 1 or differences:
        raise RunIdentityError(
            f"{label} resume refused: runtime/config/prompt/sampling hashes differ from "
            f"{path.name}: {', '.join(differences) or 'schema_version'}"
        )
    return manifest


def _run_simulator_generation(building_dir: Path, profile: str) -> None:
    command = [
        sys.executable,
        "-m",
        "simulator",
        "generate",
        "--profile",
        profile,
        "--out",
        str(building_dir),
        "--config",
        str(SIMULATOR_CONFIG),
        "--episodes-config",
        str(EPISODES_CONFIG),
        "--model-config",
        str(MODEL_CONFIG),
        "--model-doc",
        str(MODEL_DOC),
    ]
    result = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout).strip()
        raise HarnessError(f"simulator {profile} generation failed: {details}")


def _ensure_generated_base(
    root: Path,
    identity_hashes: Mapping[str, Any],
    profile: str,
) -> Path:
    base_dir = root / "base"
    generation_manifest = base_dir / "generation-manifest.json"
    if base_dir.exists():
        if not generation_manifest.is_file():
            raise HarnessError(
                f"base directory is incomplete and has no generation manifest: {base_dir}"
            )
        _validate_static_identity(
            generation_manifest,
            identity_hashes,
            label="base generation",
            profile=profile,
        )
        return base_dir

    building_dir = root / ".base-building"
    if building_dir.exists():
        raise HarnessError(
            f"incomplete simulator output exists at {building_dir}; remove it before retrying"
        )
    _run_simulator_generation(building_dir, profile)
    _write_json(
        building_dir / "generation-manifest.json",
        {
            "schema_version": 1,
            "profile": profile,
            "hashes": dict(identity_hashes),
        },
    )
    building_dir.replace(base_dir)
    return base_dir


def _input_fingerprint(database_path: Path) -> str:
    with _connection(database_path, read_only=True) as operational:
        return canonical_run_fingerprint(
            operational,
            empty_tables=("incidents", "decisions"),
        )


def _assert_base_is_frozen(database_path: Path, expected_fingerprint: str) -> None:
    with _connection(database_path, read_only=True) as operational:
        output_counts = {
            table: int(operational.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("incidents", "decisions")
        }
    fingerprint = _input_fingerprint(database_path)
    if any(output_counts.values()) or fingerprint != expected_fingerprint:
        raise RunIdentityError(
            "frozen BASE changed: expected empty incidents/decisions and input "
            f"fingerprint {expected_fingerprint}, got {output_counts} / {fingerprint}"
        )


def _episode_roles(database_path: Path) -> dict[str, tuple[str, ...]]:
    with _connection(database_path, read_only=True) as operational:
        rows = operational.execute(
            "SELECT episode_id, role FROM episodes "
            "WHERE episode_id <> 'base' ORDER BY role, episode_id"
        ).fetchall()
    roles: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        role = str(row["role"])
        if role not in ROLES:
            raise HarnessError(f"operational episode has an unknown role: {role!r}")
        roles[role].append(str(row["episode_id"]))
    if set(roles) != set(ROLES):
        raise HarnessError("base must contain development, calibration, and test episodes")
    return {role: tuple(episode_ids) for role, episode_ids in sorted(roles.items())}


def _prepare_model_monitoring(base_dir: Path) -> dict[str, Any]:
    operational_path = base_dir / "operational" / "observations.sqlite"
    artifact_root = base_dir.parent / "artifacts"
    artifact_dir = artifact_directory_for_run(MODEL_RUN_ID, data_root=artifact_root)
    with _connection(operational_path, read_only=True) as operational:
        _, training_audit = train_operational_model(
            operational,
            run_id=MODEL_RUN_ID,
            data_root=artifact_root,
            simulator_config_path=SIMULATOR_CONFIG,
            model_config_path=MODEL_CONFIG,
        )
    with _connection(operational_path, read_only=False) as operational:
        score_rows = score_monitoring_episodes(
            operational,
            model_directory=artifact_dir,
            simulator_config_path=SIMULATOR_CONFIG,
        )
        metric_rows = compute_daily_metrics(
            operational,
            simulator_config_path=SIMULATOR_CONFIG,
            monitoring_config_path=MONITORING_CONFIG,
        )
        metric_count = persist_monitoring_rows(
            operational,
            "daily_metrics",
            metric_rows,
        )
        by_episode: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in metric_rows:
            by_episode[row["episode_id"]].append(row)
        alert_count = 0
        for episode_id, rows in sorted(by_episode.items()):
            if not rows:
                continue
            last_snapshot = max(str(row["as_of_time"]) for row in rows)
            alerts = compute_alerts(
                rows,
                episode_id=episode_id,
                as_of_time=last_snapshot,
                monitoring_config_path=MONITORING_CONFIG,
            )
            alert_count += persist_monitoring_rows(
                operational,
                "alerts",
                alerts.alerts,
            )
        preexisting = {
            table: int(operational.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("incidents", "decisions")
        }
        if any(preexisting.values()):
            raise HarnessError(
                "BASE freeze point must precede all incidents/decisions; "
                f"found {preexisting}"
            )
        model_version_row = operational.execute(
            "SELECT DISTINCT model_version FROM model_scores ORDER BY model_version"
        ).fetchall()
        model_versions = sorted(str(row[0]) for row in model_version_row)
    return {
        "training_audit": training_audit,
        "score_count": len(score_rows),
        "metric_count_inserted": metric_count,
        "alert_count_inserted": alert_count,
        "model_versions": model_versions,
    }


def _freeze_base(
    base_dir: Path,
    identity_hashes: Mapping[str, Any],
    profile: str,
) -> tuple[str, dict[str, Any]]:
    operational_path = base_dir / "operational" / "observations.sqlite"
    pipeline_result = _prepare_model_monitoring(base_dir)
    artifact_dir = artifact_directory_for_run(
        MODEL_RUN_ID,
        data_root=base_dir.parent / "artifacts",
    )
    artifact_identity = model_artifact_identity(artifact_dir)
    fingerprint = _input_fingerprint(operational_path)
    manifest_path = base_dir / "base-manifest.json"
    manifest = {
        "schema_version": 1,
        "profile": profile,
        "freeze_point": "after model_scores, daily_metrics, alerts; before incidents and decisions",
        "hashes": dict(identity_hashes),
        "input_fingerprint": fingerprint,
        "model_artifacts": artifact_identity,
        "pipeline": pipeline_result,
    }
    if manifest_path.exists():
        previous = _validate_static_identity(
            manifest_path,
            identity_hashes,
            label="BASE",
            profile=profile,
        )
        if previous.get("input_fingerprint") != fingerprint:
            raise RunIdentityError("BASE input fingerprint changed while resuming the harness")
        if previous.get("model_artifacts") != artifact_identity:
            raise RunIdentityError(
                "BASE model artifact identity changed while resuming the harness"
            )
    else:
        _write_json(manifest_path, manifest)
    return fingerprint, pipeline_result


def _ensure_frozen_base(
    base_dir: Path,
    identity_hashes: Mapping[str, Any],
    profile: str,
) -> tuple[str, dict[str, Any]]:
    manifest_path = base_dir / "base-manifest.json"
    if manifest_path.exists():
        manifest = _validate_static_identity(
            manifest_path,
            identity_hashes,
            label="BASE",
            profile=profile,
        )
        artifacts = base_dir.parent / "artifacts" / MODEL_RUN_ID / "model"
        required = (
            "metadata.json",
            "predictor.joblib",
            "calibrator.joblib",
            "conformal.joblib",
            "model_scores.csv",
        )
        missing = [name for name in required if not (artifacts / name).is_file()]
        if missing:
            raise RunIdentityError(
                "frozen BASE model artifacts are missing: " + ", ".join(missing)
            )
        frozen_artifacts = manifest.get("model_artifacts")
        if not isinstance(frozen_artifacts, dict):
            raise RunIdentityError("frozen BASE manifest has no model artifact identity")
        try:
            assert_model_artifact_identity(
                artifacts,
                frozen_artifacts,
                label="frozen BASE model artifact",
            )
        except ValueError as exc:
            raise RunIdentityError(str(exc)) from exc
        fingerprint = _input_fingerprint(base_dir / "operational" / "observations.sqlite")
        if manifest.get("input_fingerprint") != fingerprint:
            raise RunIdentityError("BASE input fingerprint changed after it was frozen")
        _assert_base_is_frozen(
            base_dir / "operational" / "observations.sqlite",
            str(manifest["input_fingerprint"]),
        )
        return fingerprint, dict(manifest.get("pipeline", {}))
    return _freeze_base(base_dir, identity_hashes, profile)


def _copy_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    source_connection = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
    target_connection = sqlite3.connect(temporary)
    try:
        source_connection.backup(target_connection)
        target_connection.commit()
    finally:
        target_connection.close()
        source_connection.close()
    temporary.replace(destination)


def _policy_manifest(
    policy_name: str,
    identity: Mapping[str, Any],
    *,
    base_fingerprint: str,
    model_artifacts: Mapping[str, Any],
) -> dict[str, Any]:
    frozen_identity = {**dict(identity), "model_artifacts": dict(model_artifacts)}
    return {
        "schema_version": 1,
        **frozen_identity,
        "base_fingerprint": base_fingerprint,
        "run_fingerprint": run_fingerprint(
            frozen_identity,
            base_fingerprint=base_fingerprint,
        ),
    }


def _ensure_policy_manifest(
    policy_dir: Path,
    manifest: Mapping[str, Any],
) -> None:
    path = policy_dir / "run-manifest.json"
    policy_dir.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = _read_json(path)
        existing_hashes = existing.get("hashes")
        expected_hashes = manifest.get("hashes")
        hash_differences = (
            identity_hash_mismatches(existing_hashes, expected_hashes)
            if isinstance(existing_hashes, dict) and isinstance(expected_hashes, dict)
            else ("hashes",)
        )
        identity_fields = (
            "schema_version",
            "policy_name",
            "provider_name",
            "policy_version",
            "model_slug",
            "model_artifacts",
            "base_fingerprint",
            "run_fingerprint",
        )
        field_differences = [
            field for field in identity_fields if existing.get(field) != manifest.get(field)
        ]
        if hash_differences or field_differences:
            details = [*hash_differences, *field_differences]
            raise RunIdentityError(
                f"policy resume refused: manifest mismatch in {policy_dir}; differing "
                f"identity fields: {', '.join(details)}; "
                "use a new benchmark root for changed inputs or policy; a new root "
                "does not reset shared benchmark locks or spend"
            )
        return
    if any(policy_dir.iterdir()):
        raise HarnessError(f"policy directory has files but no run manifest: {policy_dir}")
    _write_json(path, manifest)


def _assert_policy_decisions_match_manifest(
    operational_path: Path,
    manifest: Mapping[str, Any],
) -> None:
    if not operational_path.is_file():
        return
    with _connection(operational_path, read_only=True) as operational:
        rows = operational.execute(
            "SELECT DISTINCT source, policy_version, model_slug FROM decisions "
            "ORDER BY source, policy_version, model_slug"
        ).fetchall()
    expected_version = manifest.get("policy_version")
    expected_model_slug = manifest.get("model_slug")
    mismatches = [
        (str(row[0]), row[1], row[2])
        for row in rows
        if row[1] != expected_version
        or (row[0] == "agent" and row[2] != expected_model_slug)
        or (row[0] == "rule" and row[2] is not None)
    ]
    if mismatches:
        raise RunIdentityError(
            "policy resume refused: decision rows do not match run-manifest.json; "
            f"expected policy_version={expected_version!r}, agent model_slug="
            f"{expected_model_slug!r}, found {mismatches!r}"
        )


def _decision_report_path(base_dir: Path, policy_name: str, analysis_role: str) -> Path:
    name = "decisions.json" if analysis_role == "development" else f"decisions_{analysis_role}.json"
    return (
        base_dir
        / "private"
        / "reports"
        / "p5_1"
        / policy_name
        / name
    )


def _evaluate_policy(
    operational_path: Path,
    private_path: Path,
    base_dir: Path,
    policy_name: str,
    role_episode_ids: Mapping[str, tuple[str, ...]],
    analysis_role: str,
) -> dict[str, Any]:
    selected_ids = role_episode_ids.get(analysis_role, ())
    if not selected_ids:
        raise HarnessError(f"base has no episodes in the requested role {analysis_role!r}")
    evaluation = evaluate_operational_decisions(
        operational_path,
        private_db_path=private_path,
        policy_version=str(policy_identity(policy_name)["policy_version"]),
        model_slug=policy_identity(policy_name)["model_slug"],
        episode_ids=selected_ids,
        report_path=_decision_report_path(base_dir, policy_name, analysis_role),
    )
    return {
        "episode_role": analysis_role,
        "episode_ids": list(selected_ids),
        "report_path": str(evaluation.report_path),
        "selection": evaluation.report["selection"],
        "decision_counts": evaluation.report["decision_counts"],
        "truth_counts": evaluation.report["truth_counts"],
        "unsafe_truth_rate": evaluation.report["unsafe_truth_rate"],
        "timely_routing_recall": evaluation.report["timely_routing_recall"],
    }


def _policy_run(
    root: Path,
    base_dir: Path,
    base_fingerprint: str,
    base_pipeline: Mapping[str, Any],
    identity_hashes: Mapping[str, Any],
    policy_name: str,
    analysis_role: str,
    profile: str,
) -> dict[str, Any]:
    identity = current_run_identity(policy_name, benchmark_root=root)
    hash_differences = identity_hash_mismatches(identity["hashes"], identity_hashes)
    if hash_differences:
        raise RunIdentityError(
            "policy run refused: runtime/config/prompt/sampling hashes changed while "
            f"the policy was starting: {', '.join(hash_differences)}"
        )
    model_artifacts = model_artifact_identity(
        artifact_directory_for_run(
            MODEL_RUN_ID,
            data_root=base_dir.parent / "artifacts",
        )
    )
    manifest = _policy_manifest(
        policy_name,
        identity,
        base_fingerprint=base_fingerprint,
        model_artifacts=model_artifacts,
    )
    policy_dir = root / "policies" / policy_name
    _ensure_policy_manifest(policy_dir, manifest)

    base_database = base_dir / "operational" / "observations.sqlite"
    operational_path = policy_dir / "operational" / "observations.sqlite"
    if not operational_path.exists():
        _copy_database(base_database, operational_path)
    before = _input_fingerprint(operational_path)
    if before != base_fingerprint:
        raise RunIdentityError(
            f"policy {policy_name!r} input fingerprint differs from frozen BASE"
        )
    _assert_policy_decisions_match_manifest(operational_path, manifest)

    role_episode_ids = _episode_roles(operational_path)
    role_order = ("development", "calibration", "test")
    process_roles = role_order[: role_order.index(analysis_role) + 1]
    process_episode_ids = {
        role: role_episode_ids[role]
        for role in process_roles
        if role_episode_ids.get(role)
    }
    assert_role_access(
        root,
        operation="payguard.run.policy_triage",
        role_episode_ids=process_episode_ids,
        policy_name=policy_name,
    )

    provider_name = str(POLICY_PROVIDERS[policy_name][0])
    ledger = DurableCostLedger(
        benchmark_state_directory(root) / "cost-ledger.jsonl",
        cap_usd=BENCHMARK_COST_CAP_USD,
    )
    provider = build_agent_provider(
        provider_name,
        operational_path,
        llm_config_path=LLM_CONFIG,
        cost_ledger=ledger,
        ledger_policy=policy_name,
        cost_cap_usd=BENCHMARK_COST_CAP_USD,
    )
    triage_summary = run_triage(
        policy_dir,
        simulator_config_path=SIMULATOR_CONFIG,
        triage_config_path=TRIAGE_CONFIG,
        agent_provider=provider,
        episode_ids=tuple(
            episode_id
            for role in process_roles
            for episode_id in role_episode_ids.get(role, ())
        ),
    )
    record_role_touches(
        root,
        operation="payguard.run.policy_triage",
        role_episode_ids=process_episode_ids,
        policy_name=policy_name,
        usage="operational_execution",
    )

    after = _input_fingerprint(operational_path)
    base_after = _input_fingerprint(base_database)
    try:
        _assert_base_is_frozen(base_database, base_fingerprint)
    except RunIdentityError as exc:
        raise RunIdentityError(
            f"BASE changed during policy {policy_name!r}: {exc}"
        ) from exc
    if before != after or before != base_after:
        raise RunIdentityError(
            f"input fingerprint changed during policy {policy_name!r}; BASE and policy copy differ"
        )
    verify_harness_policy_run(
        policy_dir,
        required_roles=tuple(process_roles),
    )
    evaluation = _evaluate_policy(
        operational_path,
        base_dir / "private" / "evaluator.sqlite",
        base_dir,
        policy_name,
        role_episode_ids,
        analysis_role,
    )
    cost_summary = ledger.snapshot()
    report = {
        "schema_version": 1,
        "profile": profile,
        "policy_name": policy_name,
        "policy_version": manifest["policy_version"],
        "model_slug": manifest["model_slug"],
        "run_fingerprint": manifest["run_fingerprint"],
        "base_fingerprint": base_fingerprint,
        "input_fingerprint_before": before,
        "input_fingerprint_after": after,
        "base_fingerprint_after": base_after,
        "base_pipeline": dict(base_pipeline),
        "triage": triage_summary,
        "evaluation": evaluation,
        "cost_ledger": cost_summary,
        "cost_overrun": cost_summary["overrun"],
    }
    persisted_report = {
        **report,
        "evaluation": {
            "episode_role": evaluation["episode_role"],
            "episode_ids": evaluation["episode_ids"],
            "report_path": evaluation["report_path"],
        },
    }
    _write_json(policy_dir / "run-report.json", persisted_report)
    return report


def run_benchmark(
    *,
    profile: str,
    policy_name: str,
    output_dir: str | Path,
    analysis_role: str = "development",
) -> dict[str, Any]:
    if profile not in HARNESS_PROFILES:
        raise HarnessError(f"unsupported harness profile: {profile!r}; use smoke or pilot")
    if policy_name not in POLICY_PROVIDERS:
        raise HarnessError(f"unsupported policy: {policy_name!r}")
    if analysis_role not in ROLES:
        raise HarnessError("analysis role must be development, calibration, or test")

    root = Path(output_dir).expanduser().resolve()
    state_dir = benchmark_state_directory(root, create=False)
    hashes = current_hashes(benchmark_root=root)
    _ensure_harness_root(root, hashes, state_dir, profile)
    base_dir = _ensure_generated_base(root, hashes, profile)
    base_fingerprint, base_pipeline = _ensure_frozen_base(base_dir, hashes, profile)
    roles = _episode_roles(base_dir / "operational" / "observations.sqlite")
    record_role_touches(
        root,
        operation="payguard.run.base_pipeline",
        role_episode_ids=roles,
        policy_name=None,
        usage="operational_execution",
    )
    role_episode_ids = {analysis_role: roles[analysis_role]}
    # Check role locks before policy triage can analyze or persist decisions for
    # this calibration/test evaluation request.
    assert_role_access(
        root,
        operation="payguard.run.decisions_evaluation",
        role_episode_ids=role_episode_ids,
        policy_name=policy_name,
    )
    report = _policy_run(
        root,
        base_dir,
        base_fingerprint,
        base_pipeline,
        hashes,
        policy_name,
        analysis_role,
        profile,
    )

    summary_path = root / "harness-report.json"
    if summary_path.exists():
        summary = _read_json(summary_path)
    else:
        summary = {
            "schema_version": 1,
            "profile": profile,
            "base_fingerprint": base_fingerprint,
            "policies": {},
        }
    if summary.get("base_fingerprint") != base_fingerprint:
        raise RunIdentityError("harness summary belongs to a different frozen BASE")
    policies = summary.setdefault("policies", {})
    policies[policy_name] = {
        "run_report": str(root / "policies" / policy_name / "run-report.json"),
        "run_fingerprint": report["run_fingerprint"],
        "input_fingerprint": report["input_fingerprint_after"],
        "evaluation_report": report["evaluation"]["report_path"],
        "analysis_role": analysis_role,
        "cost_overrun": report["cost_overrun"],
    }
    summary["policy_input_fingerprints"] = {
        name: value["input_fingerprint"] for name, value in sorted(policies.items())
    }
    _write_json(summary_path, summary)
    return report

