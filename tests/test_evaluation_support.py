from __future__ import annotations

import json
from pathlib import Path

from evaluation.__main__ import main as evaluation_main
import payguard.identity as identity
from payguard.identity import benchmark_state_directory
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
        "decision_id": f"decision-{decision_id * 24}",
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
        "policy_version": "fixture.policy.v1",
        "model_slug": "fake/payguard-p4-round1" if source == "agent" else None,
    }


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
    assert report["agent_support"]["uq_conditions"] == {
        "conditions_1_to_3_incidents": 1,
        "condition_4_agent_close_allowed": False,
        "conditions_1_to_4_incidents": 0,
        "agent_close_disabled": True,
    }
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
