from __future__ import annotations

import ast
import json
from pathlib import Path
import sqlite3

import pytest

from monitoring.incidents import OperationalDecisionSource, compute_incidents
from monitoring.store import persist_monitoring_rows
from simulator.asof import read_as_of
from simulator.storage import create_export_stores, insert_operational_record
from triage.decisions import persist_decisions
from triage.gate import GateConfig, evaluate_gate


T0 = "2026-01-31T17:00:00.000000Z"
DAY_0 = "2026-02-01T16:59:59.999999Z"
DAY_1 = "2026-02-02T16:59:59.999999Z"


def _metric(as_of_time: str) -> dict[str, object]:
    return {
        "metric_id": "psi.amount_avg_7d",
        "episode_id": "ep-a",
        "as_of_time": as_of_time,
        "event_at": as_of_time,
        "available_at": as_of_time,
        "business_date": "2026-02-01" if as_of_time == DAY_0 else "2026-02-02",
        "feature": "amount_avg_7d",
        "segment": "all",
        "value": 0.3,
        "reference_window": "2025-11-01/2026-01-31",
        "window_start": "2026-02-01",
        "window_end": "2026-02-01" if as_of_time == DAY_0 else "2026-02-02",
        "n_window": 30,
        "n_reference": 100,
        "flags": [],
    }


def _alert(alert_id: str, as_of_time: str) -> dict[str, object]:
    return {
        "alert_id": alert_id,
        "episode_id": "ep-a",
        "as_of_time": as_of_time,
        "metric": "psi.amount_avg_7d",
        "feature": "amount_avg_7d",
        "segment": "all",
        "value": 0.3,
        "reference": "2025-11-01/2026-01-31",
        "threshold": 0.25,
        "event_at": as_of_time,
        "available_at": as_of_time,
    }


def _monitor_as_of(operational, as_of_time: str):
    alert_rows = read_as_of(
        operational,
        "alerts",
        episode_id="ep-a",
        t0=T0,
        as_of_time=as_of_time,
    )
    source = OperationalDecisionSource(
        operational=operational,
        t0=T0,
        as_of_time=as_of_time,
    )
    return compute_incidents(alert_rows, episode_id="ep-a", decision_source=source)


def _latest(result):
    latest = {}
    for row in result.incidents:
        current = latest.get(row["incident_id"])
        if current is None or row["event_at"] > current["event_at"]:
            latest[row["incident_id"]] = row
    return latest


def test_decision_effect_is_delayed_one_snapshot_and_later_alert_is_child(tmp_path) -> None:
    stores = create_export_stores(tmp_path / "stores")
    try:
        operational = stores.operational
        insert_operational_record(
            operational, "episodes", {"episode_id": "ep-a", "role": "development"}
        )
        metric_0, alert_0 = _metric(DAY_0), _alert("alert-a", DAY_0)
        insert_operational_record(operational, "daily_metrics", metric_0)
        insert_operational_record(operational, "alerts", alert_0)

        first_result = _monitor_as_of(operational, DAY_0)
        first_rows = [row for row in first_result.incidents if row["available_at"] == DAY_0]
        assert len(first_rows) == 1
        parent_id = first_rows[0]["incident_id"]
        assert first_rows[0]["status"] == "open"
        assert persist_monitoring_rows(operational, "incidents", first_rows) == 1

        decision = evaluate_gate(
            first_rows[0],
            {
                "cause": "other",
                "runbook": None,
                "action": "human_review",
                "checklist_results": {
                    "unit_or_null_change": {
                        "status": "not_applicable",
                        "scope_match": False,
                        "window_complete": True,
                    }
                },
                "reason": "agent_not_configured",
            },
            source="agent",
            expected_episode_id="ep-a",
            as_of_time=DAY_0,
            config=GateConfig(policy_version="test-v1", close_enabled={}),
        )
        assert decision["decision_at"] == decision["available_at"] == DAY_0
        assert persist_decisions(operational, (decision,)) == 1
        assert persist_decisions(operational, (decision,)) == 0
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            operational.execute(
                "UPDATE decisions SET action='close' WHERE decision_id=?",
                (decision["decision_id"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            operational.execute(
                "DELETE FROM decisions WHERE decision_id=?",
                (decision["decision_id"],),
            )

        before_future_alert = _monitor_as_of(operational, DAY_0)
        future_metric, future_alert = _metric(DAY_1), _alert("alert-b", DAY_1)
        insert_operational_record(operational, "daily_metrics", future_metric)
        insert_operational_record(operational, "alerts", future_alert)
        after_future_alert = _monitor_as_of(operational, DAY_0)
        assert after_future_alert == before_future_alert
        assert OperationalDecisionSource(operational, T0, DAY_0).decisions_for_episode(
            "ep-a"
        ) == ()

        next_source = OperationalDecisionSource(operational, T0, DAY_1)
        effective_decisions = next_source.decisions_for_episode("ep-a")
        assert len(effective_decisions) == 1
        assert effective_decisions[0]["available_at"] == DAY_0
        assert effective_decisions[0]["decision_at"] == DAY_1

        next_result = _monitor_as_of(operational, DAY_1)
        current_rows = [
            row for row in next_result.incidents if row["available_at"] == DAY_1
        ]
        assert persist_monitoring_rows(operational, "incidents", current_rows) == 2
        latest = _latest(next_result)
        assert latest[parent_id]["status"] == "human_review"
        children = [
            row
            for row in latest.values()
            if row["parent_incident_id"] == parent_id
        ]
        assert len(children) == 1
        assert json.loads(children[0]["alert_ids_json"]) == ["alert-b"]

        day_0_incidents = read_as_of(
            operational,
            "incidents",
            episode_id="ep-a",
            t0=T0,
            as_of_time=DAY_0,
        )
        assert all(row["incident_id"] != children[0]["incident_id"] for row in day_0_incidents)
        day_1_latest = _latest(next_result)
        assert day_1_latest[parent_id]["status"] != "open"
    finally:
        stores.close()


def test_triage_runtime_avoids_evaluation_and_episode_loader_imports() -> None:
    triage_dir = Path(__file__).resolve().parents[1] / "triage"
    for source_path in triage_dir.glob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        imported_modules = []
        imported_symbols = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.append(node.module)
                imported_symbols.extend(alias.name for alias in node.names)
        assert not any(
            module == "evaluation" or module.startswith("evaluation.")
            for module in imported_modules
        ), source_path
        assert "load_episode_config" not in imported_symbols, source_path
