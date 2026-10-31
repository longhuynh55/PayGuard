from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import pytest

from evaluation.__main__ import main as evaluation_main
import evaluation.support as support
import payguard.identity as identity
from payguard.identity import benchmark_state_directory
from simulator.reproducibility import canonical_run_fingerprint
from monitoring.incidents import compute_incidents
from simulator.storage import (
    create_export_stores,
    create_store_pair,
    insert_operational_record,
    insert_private_record,
)


AS_OF = "2026-03-02T16:59:59.000000Z"


def _manifest_config() -> dict[str, object]:
    return {
        "simulator_config": {
            "timeline": {
                "t0_business_date": {"value": "2026-02-01"},
                "business_timezone": {"value": "Asia/Ho_Chi_Minh"},
            }
        },
        "episode_config": {"protocol": {"duration_days": {"value": 30}}},
    }


def _add_private_development_episode(store) -> None:
    insert_private_record(store.private, "private_episodes", {"episode_id": "ep-dev"})
    insert_private_record(
        store.private,
        "episode_manifest",
        {
            "episode_id": "ep-dev",
            "role": "development",
            "seed": 1001,
            "template": "clean",
            "content_fingerprint": "a" * 64,
            "config_fingerprint": "b" * 64,
            "config_json": json.dumps(_manifest_config(), sort_keys=True, separators=(",", ":")),
        },
    )


def _decision(decision_id: str, incident_id: str, source: str) -> dict[str, object]:
    return {
        "decision_id": f"decision-{hashlib.sha256(decision_id.encode()).hexdigest()[:24]}",
        "incident_id": incident_id,
        "episode_id": "ep-dev",
        "decision_at": AS_OF,
        "as_of_time": AS_OF,
        "event_at": AS_OF,
        "available_at": AS_OF,
        "source": source,
        "runbook": "seasonal_calendar",
        "cause": "seasonal",
        "evidence_status": "sufficient",
        "action": "human_review",
        "scope_json": {"feature": "txn_count", "segment": "all", "start_at": AS_OF, "end_at": AS_OF},
        "checklist_results_json": {
            "seasonal_calendar": {
                "status": "pass",
                "scope_match": True,
                "window_complete": True,
                "requires_human_review": False,
            },
            "fraud_signal": {
                "status": "fail",
                "scope_match": False,
                "window_complete": True,
                "requires_human_review": False,
            },
        },
        "tool_trace_ids_json": [],
        "conclusion": "Fixture decision.",
        "reason": "Agent closing is disabled.",
        "close_score": 1.0 if source == "agent" else None,
        "policy_version": identity.policy_identity("rule_agent_fake")["policy_version"],
        "model_slug": "fake/payguard-p4-round1" if source == "agent" else None,
    }


def _support_harness(
    tmp_path: Path, monkeypatch, *, operational_role: str = "development"
):
    benchmark_root = tmp_path / "benchmark"
    policy_name = "rule_agent_fake"
    policy_run = benchmark_root / "policies" / policy_name
    state_dir = tmp_path / "benchmark-state"
    monkeypatch.setenv(identity.BENCHMARK_STATE_DIR_ENV, str(state_dir))
    (benchmark_root / "base").mkdir(parents=True)
    policy_run.mkdir(parents=True)
    (benchmark_root / "harness-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profile": "smoke",
                "state_dir": str(state_dir.resolve()),
            }
        ),
        encoding="utf-8",
    )
    (policy_run / "run-manifest.json").write_text(
        json.dumps({"policy_name": policy_name}), encoding="utf-8"
    )
    base_store = create_export_stores(benchmark_root / "base")
    policy_store = create_store_pair(
        policy_run / "operational" / "observations.sqlite",
        tmp_path / "unused-policy-private.sqlite",
    )
    _add_private_development_episode(base_store)
    insert_operational_record(
        policy_store.operational,
        "episodes",
        {"episode_id": "ep-dev", "role": operational_role},
    )
    return benchmark_root, policy_run, base_store, policy_store


def _finalize_support_harness(
    benchmark_root: Path, policy_run: Path, state_dir: Path
) -> None:
    """Freeze a BASE copy and write the identity manifests used by the CLI guard."""
    base_operational = benchmark_root / "base" / "operational" / "observations.sqlite"
    policy_operational = policy_run / "operational" / "observations.sqlite"
    shutil.copyfile(policy_operational, base_operational)
    connection = sqlite3.connect(base_operational)
    try:
        triggers = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name IN ('decisions', 'incidents')"
        ).fetchall()
        for (trigger_name,) in triggers:
            escaped = str(trigger_name).replace('"', '""')
            connection.execute(f'DROP TRIGGER "{escaped}"')
        connection.execute("DELETE FROM decisions")
        connection.execute("DELETE FROM incidents")
        connection.commit()
    finally:
        connection.close()
    with sqlite3.connect(base_operational) as connection:
        connection.row_factory = sqlite3.Row
        base_fingerprint = canonical_run_fingerprint(
            connection, empty_tables=("incidents", "decisions")
        )
    hashes = identity.current_hashes(benchmark_root=benchmark_root)
    (benchmark_root / "harness-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profile": "smoke",
                "state_dir": str(state_dir.resolve()),
                "hashes": hashes,
            }
        ),
        encoding="utf-8",
    )
    (benchmark_root / "base" / "base-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profile": "smoke",
                "hashes": hashes,
                "input_fingerprint": base_fingerprint,
            }
        ),
        encoding="utf-8",
    )
    policy_identity = identity.current_run_identity(
        "rule_agent_fake", benchmark_root=benchmark_root
    )
    (policy_run / "run-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                **policy_identity,
                "base_fingerprint": base_fingerprint,
                "run_fingerprint": identity.run_fingerprint(
                    policy_identity, base_fingerprint=base_fingerprint
                ),
            }
        ),
        encoding="utf-8",
    )


def _insert_alert_incident(
    policy_store,
    *,
    alert_id: str,
    feature: str = "txn_count",
    as_of_time: str = AS_OF,
) -> dict[str, object]:
    metric_id = "volume.txn_count" if feature == "txn_count" else f"psi.{feature}"
    insert_operational_record(
        policy_store.operational,
        "daily_metrics",
        {
            "metric_id": metric_id,
            "episode_id": "ep-dev",
            "as_of_time": as_of_time,
            "event_at": as_of_time,
            "available_at": as_of_time,
            "business_date": as_of_time[:10],
            "feature": feature,
            "segment": "all",
            "value": 1.0,
            "reference_window": "2025-11-01/2026-01-31",
            "window_start": as_of_time[:10],
            "window_end": as_of_time[:10],
            "n_window": 40,
            "n_reference": 100,
            "flags": [],
        },
    )
    alert = {
        "alert_id": alert_id,
        "episode_id": "ep-dev",
        "as_of_time": as_of_time,
        "metric": metric_id,
        "feature": feature,
        "segment": "all",
        "value": 1.0,
        "reference": "2025-11-01/2026-01-31",
        "threshold": 4.25,
        "event_at": as_of_time,
        "available_at": as_of_time,
    }
    insert_operational_record(policy_store.operational, "alerts", alert)
    incident_rows = compute_incidents([alert], episode_id="ep-dev").incidents
    assert len(incident_rows) == 1
    for incident in incident_rows:
        insert_operational_record(policy_store.operational, "incidents", incident)
    return incident_rows[0]


def _add_truth(
    base_store,
    *,
    truth_id: str,
    cause: str,
    requires_intervention: bool,
    rule_covered: bool,
    parent_scenario: str,
    features: list[str],
) -> None:
    insert_private_record(
        base_store.private,
        "incident_truth",
        {
            "incident_truth_id": truth_id,
            "episode_id": "ep-dev",
            "cause": cause,
            "requires_intervention": int(requires_intervention),
            "mechanism": "functional-support-fixture",
            "rule_covered": int(rule_covered),
            "affected_entities_json": [],
            "affected_segments_json": ["all"],
            "affected_features_json": features,
            "effect_start_at": AS_OF,
            "effect_end_at": None,
            "effect_available_at": AS_OF,
            "intervention_start_at": AS_OF,
            "deadline_at": AS_OF,
            "accepted_actions_json": ["human_review"] if requires_intervention else [],
            "parent_scenario": parent_scenario,
        },
    )


def test_support_cli_is_development_only_private_and_byte_reproducible(
    tmp_path: Path, monkeypatch
) -> None:
    benchmark_root = tmp_path / "benchmark"
    policy_name = "rule_agent_fake"
    policy_run = benchmark_root / "policies" / policy_name
    state_dir = tmp_path / "benchmark-state"
    monkeypatch.setenv(identity.BENCHMARK_STATE_DIR_ENV, str(state_dir))
    (benchmark_root / "base").mkdir(parents=True)
    policy_run.mkdir(parents=True)
    (benchmark_root / "harness-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profile": "smoke",
                "state_dir": str(state_dir.resolve()),
            }
        ),
        encoding="utf-8",
    )
    (policy_run / "run-manifest.json").write_text(
        json.dumps({"policy_name": policy_name}), encoding="utf-8"
    )

    base_store = create_export_stores(benchmark_root / "base")
    policy_store = create_store_pair(
        policy_run / "operational" / "observations.sqlite",
        tmp_path / "unused-policy-private.sqlite",
    )
    try:
        _add_private_development_episode(base_store)
        insert_operational_record(
            policy_store.operational,
            "episodes",
            {"episode_id": "ep-dev", "role": "development"},
        )
        insert_operational_record(
            policy_store.operational,
            "episodes",
            {"episode_id": "ep-heldout", "role": "calibration"},
        )
        metric = {
            "metric_id": "volume.txn_count",
            "episode_id": "ep-dev",
            "as_of_time": AS_OF,
            "event_at": AS_OF,
            "available_at": AS_OF,
            "business_date": "2026-03-02",
            "feature": "txn_count",
            "segment": "all",
            "value": 0.0,
            "reference_window": "2025-11-01/2026-01-31",
            "window_start": "2026-03-02",
            "window_end": "2026-03-02",
            "n_window": 40,
            "n_reference": 100,
            "flags": [],
        }
        insert_operational_record(policy_store.operational, "daily_metrics", metric)
        alert = {
            "alert_id": "alert-development-background",
            "episode_id": "ep-dev",
            "as_of_time": AS_OF,
            "metric": "volume.txn_count",
            "feature": "txn_count",
            "segment": "all",
            "value": 1.0,
            "reference": "2025-11-01/2026-01-31",
            "threshold": 4.25,
            "event_at": AS_OF,
            "available_at": AS_OF,
        }
        insert_operational_record(policy_store.operational, "alerts", alert)
        incident_rows = compute_incidents([alert], episode_id="ep-dev").incidents
        for incident in incident_rows:
            insert_operational_record(policy_store.operational, "incidents", incident)
        incident_id = incident_rows[0]["incident_id"]
        insert_operational_record(
            policy_store.operational, "decisions", _decision("1", incident_id, "rule")
        )
        insert_operational_record(
            policy_store.operational, "decisions", _decision("2", incident_id, "agent")
        )
        base_store.commit()
        policy_store.commit()
    finally:
        base_store.close()
        policy_store.close()

    _finalize_support_harness(benchmark_root, policy_run, state_dir)

    args = ["support", "--run", str(policy_run)]
    assert evaluation_main(args) == 0
    report_path = (
        benchmark_root
        / "base"
        / "private"
        / "reports"
        / "p5_0b"
        / policy_name
        / "development_support.json"
    )
    first_report = report_path.read_bytes()
    report = json.loads(first_report)
    assert report["episode_role"] == "development"
    assert report["episode_ids"] == ["ep-dev"]
    assert report["incident_counts"]["unmatched"] == 1
    assert report["background_workload"]["unmatched_alerts"] == 1
    assert report["agent_support"]["incidents_reaching_agent"] == 1
    assert report["agent_support"]["close_eligibility"]["rule_path"] == {
        "checklist_eligible_incidents": 1,
        "close_enabled": False,
        "authorized_to_close_now": 0,
    }
    assert report["agent_support"]["close_eligibility"]["agent_path"][
        "checklist_eligible_incidents"
    ] == 1
    assert {
        key: value
        for key, value in report["agent_support"]["uq_conditions"].items()
        if key != "by_close_enabled"
    } == {
        "conditions_1_to_3_incidents": 1,
        "condition_4_agent_close_allowed": False,
        "conditions_1_to_4_incidents": 0,
        "agent_close_disabled": True,
    }
    assert report["agent_support"]["uq_conditions"]["by_close_enabled"] == {
        "off": {
            "conditions_1_to_3_incidents": 1,
            "conditions_1_to_3_reason": None,
            "conditions_1_to_4_incidents": 0,
            "conditions_1_to_4_reason": None,
        },
        "on": {
            "conditions_1_to_3_incidents": 0,
            "conditions_1_to_3_reason": None,
            "conditions_1_to_4_incidents": 0,
            "conditions_1_to_4_reason": None,
        },
    }
    background_group = next(
        group
        for group in report["agent_support"]["by_truth_group"]
        if group["group_type"] == "unmatched_background"
    )
    assert background_group["incidents_reaching_agent"] == 1
    assert any(
        group["group_type"] == "ambiguous"
        for group in report["agent_support"]["by_truth_group"]
    )
    assert report["interpretation"]["seasonal_calendar_checklist_available"] is False
    assert "round-1 runtime" in report["interpretation"][
        "seasonal_calendar_checklist_availability_reason"
    ]
    assert report_path.is_relative_to(benchmark_root / "base" / "private" / "reports")

    role_ledger = json.loads(
        (benchmark_state_directory(benchmark_root) / "seed-role-ledger.json").read_text(
            encoding="utf-8"
        )
    )
    support_entries = [
        entry
        for entry in role_ledger["entries"].values()
        if entry["operation"] == "evaluation.support.development"
    ]
    assert len(support_entries) == 1
    assert support_entries[0]["roles"] == {"development": ["ep-dev"]}

    assert evaluation_main(args) == 0
    assert report_path.read_bytes() == first_report


@pytest.mark.parametrize("failure_mode", ["checklist-error", "runner-error"])
def test_support_cli_excludes_tool_failures_from_uq_conditions(
    failure_mode: str, tmp_path: Path, monkeypatch
) -> None:
    benchmark_root, policy_run, base_store, policy_store = _support_harness(
        tmp_path, monkeypatch
    )
    try:
        incident = _insert_alert_incident(
            policy_store, alert_id="alert-tool-error", feature="txn_count"
        )
        decision = _decision("error", str(incident["incident_id"]), "agent")
        checklists = decision["checklist_results_json"]
        assert isinstance(checklists, dict)
        if failure_mode == "checklist-error":
            checklists["agent_run_sql"] = {
                "status": "error",
                "scope_match": True,
                "window_complete": True,
                "requires_human_review": False,
            }
            assert "__runner_guard" not in checklists
        else:
            checklists["agent_run_sql"] = {
                "status": "pass",
                "scope_match": True,
                "window_complete": True,
                "requires_human_review": False,
            }
            checklists["__runner_guard"] = {
                "status": "not_applicable",
                "details": {
                    "forced_human": True,
                    "reason_codes": ["runner_tool_error"],
                },
            }
        decision["evidence_status"] = "insufficient"
        decision["reason"] = "A tool reported an error."
        insert_operational_record(policy_store.operational, "decisions", decision)
        base_store.commit()
        policy_store.commit()
    finally:
        base_store.close()
        policy_store.close()

    _finalize_support_harness(
        benchmark_root, policy_run, tmp_path / "benchmark-state"
    )

    assert evaluation_main(["support", "--run", str(policy_run)]) == 0
    report_path = (
        benchmark_root
        / "base"
        / "private"
        / "reports"
        / "p5_0b"
        / "rule_agent_fake"
        / "development_support.json"
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["agent_support"]["uq_conditions"][
        "conditions_1_to_3_incidents"
    ] == 0
    assert report["agent_support"]["uq_conditions"]["by_close_enabled"]["off"][
        "conditions_1_to_3_incidents"
    ] == 0


def test_support_cli_reports_null_for_unmeasurable_close_enabled_on_state(
    tmp_path: Path, monkeypatch
) -> None:
    benchmark_root, policy_run, base_store, policy_store = _support_harness(
        tmp_path, monkeypatch
    )
    try:
        incident = _insert_alert_incident(
            policy_store, alert_id="alert-seasonal-agent", feature="txn_count"
        )
        _add_truth(
            base_store,
            truth_id="truth-seasonal-on-unmeasurable",
            cause="seasonal",
            requires_intervention=False,
            rule_covered=False,
            parent_scenario="on-unmeasurable",
            features=["txn_count"],
        )
        decision = _decision("seasonal", str(incident["incident_id"]), "agent")
        insert_operational_record(policy_store.operational, "decisions", decision)
        base_store.commit()
        policy_store.commit()
    finally:
        base_store.close()
        policy_store.close()

    _finalize_support_harness(
        benchmark_root, policy_run, tmp_path / "benchmark-state"
    )

    assert evaluation_main(["support", "--run", str(policy_run)]) == 0
    report_path = (
        benchmark_root
        / "base"
        / "private"
        / "reports"
        / "p5_0b"
        / "rule_agent_fake"
        / "development_support.json"
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    on_state = report["agent_support"]["uq_conditions"]["by_close_enabled"]["on"]
    assert report["agent_support"]["uq_conditions"][
        "conditions_1_to_3_incidents"
    ] == 1
    assert on_state["conditions_1_to_3_incidents"] is None
    assert on_state["conditions_1_to_3_reason"]
    truth_group = next(
        row
        for row in report["agent_support"]["by_truth_group"]
        if row["group_type"] == "truth" and row["cause"] == "seasonal"
    )
    assert truth_group["by_close_enabled"]["on"][
        "conditions_1_to_3_incidents"
    ] is None
    assert truth_group["by_close_enabled"]["on"]["conditions_1_to_3_reason"]


@pytest.mark.parametrize(
    ("case", "rule_time", "agent_time", "different_agent_start", "expected_on"),
    [
        ("same-start-prior-rule", "2026-03-01T16:59:59.000000Z", AS_OF, False, 0),
        ("different-start-prior-rule", "2026-03-01T16:59:59.000000Z", AS_OF, True, 0),
        (
            "future-rule",
            AS_OF,
            "2026-03-02T16:58:59.000000Z",
            False,
            1,
        ),
    ],
)
def test_support_cli_on_projection_matches_feature_segment_and_prior_time_only(
    case: str,
    rule_time: str,
    agent_time: str,
    different_agent_start: bool,
    expected_on: int,
    tmp_path: Path,
    monkeypatch,
) -> None:
    benchmark_root, policy_run, base_store, policy_store = _support_harness(
        tmp_path, monkeypatch
    )
    try:
        incident = _insert_alert_incident(
            policy_store,
            alert_id="alert-on-projection",
            feature="txn_count",
            as_of_time="2026-02-28T16:59:59.000000Z",
        )
        incident_id = str(incident["incident_id"])
        same_scope_start = str(incident["opened_at"])
        rule = _decision("prior-rule", incident_id, "rule")
        agent = _decision("later-agent", incident_id, "agent")
        for decision, decision_time in ((rule, rule_time), (agent, agent_time)):
            for field in ("decision_at", "as_of_time", "event_at", "available_at"):
                decision[field] = decision_time
            start_at = same_scope_start
            if decision is agent and different_agent_start:
                start_at = "2026-02-27T16:59:59.000000Z"
            decision["scope_json"] = {
                "feature": "txn_count",
                "segment": "all",
                "start_at": start_at,
                "end_at": decision_time,
            }
        insert_operational_record(policy_store.operational, "decisions", rule)
        insert_operational_record(policy_store.operational, "decisions", agent)
        base_store.commit()
        policy_store.commit()
    finally:
        base_store.close()
        policy_store.close()

    _finalize_support_harness(
        benchmark_root, policy_run, tmp_path / "benchmark-state"
    )
    assert evaluation_main(["support", "--run", str(policy_run)]) == 0
    report_path = (
        benchmark_root
        / "base"
        / "private"
        / "reports"
        / "p5_0b"
        / "rule_agent_fake"
        / "development_support.json"
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["agent_support"]["uq_conditions"]["by_close_enabled"]["on"][
        "conditions_1_to_3_incidents"
    ] == expected_on


def test_support_cli_emits_rows_for_truth_groups_without_matched_incidents(
    tmp_path: Path, monkeypatch
) -> None:
    benchmark_root, policy_run, base_store, policy_store = _support_harness(
        tmp_path, monkeypatch
    )
    try:
        _add_truth(
            base_store,
            truth_id="truth-unmatched-seasonal",
            cause="seasonal",
            requires_intervention=False,
            rule_covered=False,
            parent_scenario="unmatched-two-cause",
            features=["amount"],
        )
        _add_truth(
            base_store,
            truth_id="truth-unmatched-fraud",
            cause="fraud_surge",
            requires_intervention=True,
            rule_covered=False,
            parent_scenario="unmatched-two-cause",
            features=["amount"],
        )
        incident = _insert_alert_incident(
            policy_store, alert_id="alert-unmatched-truth-group", feature="txn_count"
        )
        decision = _decision("unmatched-agent", str(incident["incident_id"]), "agent")
        insert_operational_record(policy_store.operational, "decisions", decision)
        base_store.commit()
        policy_store.commit()
    finally:
        base_store.close()
        policy_store.close()

    _finalize_support_harness(
        benchmark_root, policy_run, tmp_path / "benchmark-state"
    )
    assert evaluation_main(["support", "--run", str(policy_run)]) == 0
    report_path = (
        benchmark_root
        / "base"
        / "private"
        / "reports"
        / "p5_0b"
        / "rule_agent_fake"
        / "development_support.json"
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["incident_counts"]["unmatched"] == 1
    truth_groups = {
        row["cause"]: row
        for row in report["agent_support"]["by_truth_group"]
        if row["group_type"] == "truth"
    }
    assert set(truth_groups) == {"seasonal", "fraud_surge"}
    assert all(row["incidents_reaching_agent"] == 0 for row in truth_groups.values())


def test_support_cli_refuses_role_mismatch_before_truth_or_role_ledger(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    benchmark_root, policy_run, base_store, policy_store = _support_harness(
        tmp_path, monkeypatch, operational_role="calibration"
    )
    try:
        base_store.commit()
        policy_store.commit()
    finally:
        base_store.close()
        policy_store.close()

    _finalize_support_harness(
        benchmark_root, policy_run, tmp_path / "benchmark-state"
    )

    truth_loads: list[tuple[str, ...]] = []

    def tracked_truth_loader(path, *, episode_ids):
        truth_loads.append(tuple(episode_ids))
        raise AssertionError("truth must not be loaded after a role mismatch")

    monkeypatch.setattr(support, "load_private_evaluation_data", tracked_truth_loader)
    ledger_path = benchmark_state_directory(benchmark_root) / "seed-role-ledger.json"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_before = json.dumps(
        {"schema_version": 1, "entries": {"preserved-entry": {"operation": "fixture"}}}
    ).encode("utf-8")
    ledger_path.write_bytes(ledger_before)

    assert evaluation_main(["support", "--run", str(policy_run)]) == 2
    captured = capsys.readouterr()
    assert "roles disagree with the private episode_manifest.role" in captured.err
    assert truth_loads == []
    assert ledger_path.read_bytes() == ledger_before


def test_support_cli_rejects_saved_checklist_without_status_as_support_error(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    benchmark_root, policy_run, base_store, policy_store = _support_harness(
        tmp_path, monkeypatch
    )
    try:
        incident = _insert_alert_incident(
            policy_store, alert_id="alert-missing-checklist-status"
        )
        decision = _decision("missing-status", str(incident["incident_id"]), "agent")
        insert_operational_record(policy_store.operational, "decisions", decision)
        base_store.commit()
        policy_store.commit()
    finally:
        base_store.close()
        policy_store.close()

    operational_path = policy_run / "operational" / "observations.sqlite"
    connection = sqlite3.connect(operational_path)
    try:
        triggers = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='decisions'"
        ).fetchall()
        for (trigger_name,) in triggers:
            escaped = str(trigger_name).replace('"', '""')
            connection.execute(f'DROP TRIGGER "{escaped}"')
        row = connection.execute(
            "SELECT decision_id, checklist_results_json FROM decisions"
        ).fetchone()
        checklists = json.loads(row[1])
        del checklists["seasonal_calendar"]["status"]
        connection.execute(
            "UPDATE decisions SET checklist_results_json=? WHERE decision_id=?",
            (json.dumps(checklists), row[0]),
        )
        connection.commit()
    finally:
        connection.close()
    _finalize_support_harness(
        benchmark_root, policy_run, tmp_path / "benchmark-state"
    )

    assert evaluation_main(["support", "--run", str(policy_run)]) == 2
    captured = capsys.readouterr()
    assert "DevelopmentSupportError" in captured.err
    assert "missing a valid status" in captured.err
    assert not (
        benchmark_root
        / "base"
        / "private"
        / "reports"
        / "p5_0b"
        / "rule_agent_fake"
        / "development_support.json"
    ).exists()


def test_support_cli_groups_matched_mixed_parent_truth_and_close_enabled_states(
    tmp_path: Path, monkeypatch
) -> None:
    benchmark_root, policy_run, base_store, policy_store = _support_harness(
        tmp_path, monkeypatch
    )
    try:
        _add_truth(
            base_store,
            truth_id="truth-seasonal-benign",
            cause="seasonal",
            requires_intervention=False,
            rule_covered=True,
            parent_scenario="mixed-parent",
            features=["txn_count", "amount"],
        )
        _add_truth(
            base_store,
            truth_id="truth-fraud-surge",
            cause="fraud_surge",
            requires_intervention=True,
            rule_covered=False,
            parent_scenario="mixed-parent",
            features=["txn_count", "amount"],
        )

        incident_ids = []
        for index, feature in enumerate(("txn_count", "amount_avg_7d"), start=1):
            incident = _insert_alert_incident(
                policy_store,
                alert_id=f"alert-mixed-{index}",
                feature=feature,
            )
            incident_id = str(incident["incident_id"])
            incident_ids.append(incident_id)
            rule = _decision(f"r{index}", incident_id, "rule")
            agent = _decision(f"a{index}", incident_id, "agent")
            rule["scope_json"] = {
                "feature": feature,
                "segment": "all",
                "start_at": AS_OF,
                "end_at": AS_OF,
            }
            agent["scope_json"] = {
                "feature": feature,
                "segment": "merchant:42" if index == 2 else "all",
                "start_at": AS_OF,
                "end_at": AS_OF,
            }
            insert_operational_record(policy_store.operational, "decisions", rule)
            insert_operational_record(policy_store.operational, "decisions", agent)

        base_store.commit()
        policy_store.commit()
    finally:
        base_store.close()
        policy_store.close()

    _finalize_support_harness(
        benchmark_root, policy_run, tmp_path / "benchmark-state"
    )

    assert evaluation_main(["support", "--run", str(policy_run)]) == 0
    report_path = (
        benchmark_root
        / "base"
        / "private"
        / "reports"
        / "p5_0b"
        / "rule_agent_fake"
        / "development_support.json"
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["incident_counts"]["matched"] == 2
    assert report["truth_counts"]["total"] == 2
    assert report["rule_coverage"]["rule_covered_truths"] == 1
    by_cause = {
        row["cause"]: row for row in report["truth_counts"]["by_episode_and_cause"]
    }
    assert by_cause["seasonal"]["matched_truths"] == 1
    assert by_cause["seasonal"]["rule_covered_truths"] == 1
    assert by_cause["seasonal"]["matched_runbooks"] == ["seasonal_calendar"]
    assert by_cause["fraud_surge"]["matched_truths"] == 1
    assert by_cause["fraud_surge"]["matched_runbooks"] == ["seasonal_calendar"]
    assert report["rule_coverage"]["matched_runbooks"] == [
        {
            "runbook": "seasonal_calendar",
            "incident_count": 2,
            "incident_ids": sorted(incident_ids),
            "matched_truth_ids": sorted(
                ["truth-seasonal-benign", "truth-fraud-surge"]
            ),
        }
    ]
    assert report["seasonal_benign_support"]["seasonal_benign_truths"] == 1
    assert report["seasonal_benign_support"]["matched_seasonal_benign_truths"] == 0

    truth_groups = {
        row["cause"]: row
        for row in report["agent_support"]["by_truth_group"]
        if row["group_type"] == "truth"
    }
    assert set(truth_groups) == {"seasonal", "fraud_surge"}
    assert truth_groups["seasonal"]["episode_id"] == "ep-dev"
    assert truth_groups["seasonal"]["has_parent_scenario"] is True
    assert truth_groups["seasonal"]["template"] == "clean"
    assert truth_groups["seasonal"]["incidents_reaching_agent"] == 2
    assert truth_groups["seasonal"]["conditions_1_to_3_incidents"] == 2
    assert truth_groups["seasonal"]["conditions_1_to_4_incidents"] == 0
    assert truth_groups["fraud_surge"]["incidents_reaching_agent"] == 2
    assert truth_groups["fraud_surge"]["conditions_1_to_3_incidents"] == 2
    by_close_enabled = report["agent_support"]["uq_conditions"]["by_close_enabled"]
    assert by_close_enabled["off"]["conditions_1_to_3_incidents"] == 2
    assert by_close_enabled["on"]["conditions_1_to_3_incidents"] == 1
