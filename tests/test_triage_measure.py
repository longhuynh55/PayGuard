from __future__ import annotations

from dataclasses import replace
import json
import sqlite3
from pathlib import Path

import pytest

from monitoring.store import persist_monitoring_rows
from simulator.storage import StorageError, create_export_stores, insert_operational_record
from triage.agent import OneStepAgent
from triage.llm import FakeLLMClient, load_llm_config
from triage.measure import MeasurementError, measure_triage


DAY_0 = "2026-02-01T16:59:59.999999Z"
DAY_1 = "2026-02-02T16:59:59.999999Z"
DAY_2 = "2026-02-03T16:59:59.999999Z"


def _metric(episode_id: str) -> dict[str, object]:
    return {
        "metric_id": "psi.amount_avg_7d",
        "episode_id": episode_id,
        "as_of_time": DAY_0,
        "event_at": DAY_0,
        "available_at": DAY_0,
        "business_date": "2026-02-01",
        "feature": "amount_avg_7d",
        "segment": "all",
        "value": 0.3,
        "reference_window": "2025-11-01/2026-01-31",
        "window_start": "2026-02-01",
        "window_end": "2026-02-01",
        "n_window": 30,
        "n_reference": 100,
        "flags": [],
    }


def _alert(episode_id: str) -> dict[str, object]:
    return {
        "alert_id": f"alert-{episode_id}",
        "episode_id": episode_id,
        "as_of_time": DAY_0,
        "metric": "psi.amount_avg_7d",
        "feature": "amount_avg_7d",
        "segment": "all",
        "value": 0.3,
        "reference": "2025-11-01/2026-01-31",
        "threshold": 0.25,
        "event_at": DAY_0,
        "available_at": DAY_0,
    }


def _metric_on(episode_id: str, as_of_time: str) -> dict[str, object]:
    return {
        **_metric(episode_id),
        "as_of_time": as_of_time,
        "event_at": as_of_time,
        "available_at": as_of_time,
        "business_date": as_of_time[:10],
        "window_start": as_of_time[:10],
        "window_end": as_of_time[:10],
    }


def _alert_on(episode_id: str, as_of_time: str) -> dict[str, object]:
    return {
        **_alert(episode_id),
        "alert_id": f"alert-{episode_id}-{as_of_time[:10]}",
        "as_of_time": as_of_time,
        "event_at": as_of_time,
        "available_at": as_of_time,
    }


def _make_run(run_dir: Path, episodes: tuple[tuple[str, str], ...]) -> Path:
    with create_export_stores(run_dir) as stores:
        metrics = []
        alerts = []
        for episode_id, role in episodes:
            insert_operational_record(
                stores.operational,
                "episodes",
                {"episode_id": episode_id, "role": role},
            )
            metrics.append(_metric(episode_id))
            alerts.append(_alert(episode_id))
        persist_monitoring_rows(stores.operational, "daily_metrics", metrics)
        persist_monitoring_rows(stores.operational, "alerts", alerts)
        database_path = stores.operational_path
    return database_path


def _fake_provider(database_path: Path, *, config=None, before_request=None):
    def response_factory(payload):
        if before_request is not None:
            before_request()
        prompt = payload["messages"][0]["content"]
        if "field named sql" in prompt:
            return {"sql": "SELECT COUNT(*) AS transaction_count FROM asof.transactions"}
        return {
            "cause": "other",
            "runbook": None,
            "action": "recheck",
            "reason": "A later snapshot may add evidence.",
        }

    client = FakeLLMClient(response_factory=response_factory, config=config)
    return OneStepAgent(database_path, client), client


def _run_measure(run_dir: Path, provider: OneStepAgent, out: Path, *, count: int = 3):
    return measure_triage(
        run_dir,
        agent_provider=provider,
        provider_name="fake",
        incidents=count,
        out=out,
    )


def test_measure_selects_development_first_falls_back_to_calibration_and_runs_once(
    tmp_path, monkeypatch
) -> None:
    import urllib.request

    def no_network(*_args, **_kwargs):
        raise AssertionError("fake provider attempted network access")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    run_dir = tmp_path / "measurement-run"
    database_path = _make_run(
        run_dir,
        (
            ("ep-test-000", "test"),
            ("ep-dev-b", "development"),
            ("ep-cal-a", "calibration"),
            ("ep-dev-a", "development"),
        ),
    )
    report_path = run_dir / "p4_6_tokens.json"
    initial_report_checked = False

    def check_initial_report():
        nonlocal initial_report_checked
        if initial_report_checked:
            return
        initial = json.loads(report_path.read_text(encoding="utf-8"))
        assert initial["status"] == "running"
        assert len(initial["selected_incidents"]) == 3
        assert initial["calls"] == []
        initial_report_checked = True

    provider, client = _fake_provider(
        database_path, before_request=check_initial_report
    )

    report = _run_measure(run_dir, provider, report_path)

    selected = report["selected_incidents"]
    assert [entry["episode_id"] for entry in selected] == [
        "ep-dev-a",
        "ep-dev-b",
        "ep-cal-a",
    ]
    assert all(entry["episode_id"] != "ep-test-000" for entry in selected)
    assert len(client.requests) == 6
    assert report["status"] == "complete"
    assert report["totals"]["call_count"] == 6
    assert report["totals"]["input_tokens"] == 48
    assert report["totals"]["output_tokens"] == 24
    assert report["totals"]["total_tokens"] == 72
    assert [item["call_count"] for item in report["incident_totals"]] == [2, 2, 2]
    assert all(
        {"input_tokens", "output_tokens", "total_tokens", "cost_usd", "latency_ms",
         "model_canonical_slug", "error_status"}.issubset(call)
        for call in report["calls"]
    )
    assert all(call["error_status"] is None for call in report["calls"])
    assert report["extrapolation"]["design_calls_per_agent_incident"] == 60
    assert report["extrapolation"]["estimated_cost_per_agent_incident_usd"] is not None

    serialized = json.dumps(report, ensure_ascii=False).lower()
    assert "truth" not in serialized
    assert "accepted_actions" not in serialized
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT source, action, COUNT(*) FROM decisions GROUP BY source, action"
        ).fetchall()
    assert rows == [("agent", "recheck", 3)]


def test_measure_refuses_run_with_prior_agent_decisions(tmp_path) -> None:
    run_dir = tmp_path / "measure-freshness"
    database_path = _make_run(
        run_dir,
        (("ep-dev-a", "development"), ("ep-dev-b", "development"),
         ("ep-dev-c", "development")),
    )
    provider, _ = _fake_provider(database_path)
    _run_measure(run_dir, provider, run_dir / "first.json")

    replay_provider, replay_client = _fake_provider(database_path)
    with pytest.raises(StorageError, match="use a fresh copy of the run directory"):
        _run_measure(run_dir, replay_provider, run_dir / "second.json")

    assert replay_client.requests == []


def test_measure_default_fails_fast_when_fewer_incidents_are_available(tmp_path) -> None:
    run_dir = tmp_path / "measure-underfilled"
    database_path = _make_run(
        run_dir,
        (("ep-dev-a", "development"), ("ep-dev-b", "development")),
    )
    provider, client = _fake_provider(database_path)
    report_path = run_dir / "underfilled.json"

    with pytest.raises(MeasurementError, match="only 2 agent-eligible"):
        _run_measure(run_dir, provider, report_path)

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["requested_incidents"] == 3
    assert report["available_incidents"] == 2
    assert len(report["selected_incidents"]) == 2
    assert report["calls"] == []
    assert client.requests == []


def test_measure_budget_stop_blocks_further_requests_and_records_status(tmp_path) -> None:
    run_dir = tmp_path / "measure-budget"
    database_path = _make_run(
        run_dir,
        (("ep-dev-a", "development"), ("ep-dev-b", "development"),
         ("ep-dev-c", "development")),
    )
    tiny_budget = replace(load_llm_config(), max_total_cost_usd=0.00000001, retry_count=0)
    provider, client = _fake_provider(database_path, config=tiny_budget)

    report = _run_measure(run_dir, provider, run_dir / "budget.json")

    assert report["status"] == "budget_stopped"
    assert report["error_status"] == "cost_limit_exceeded"
    assert len(report["selected_incidents"]) == 3
    assert len(report["incident_totals"]) == 1
    assert len(client.requests) == 1
    assert report["calls"][0]["error_status"] == "cost_limit_exceeded"
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM decisions WHERE source='agent'"
        ).fetchone()[0] == 1


def test_measure_persists_incident_snapshots_only_through_latest_selected_as_of(
    tmp_path,
) -> None:
    run_dir = tmp_path / "measure-snapshot-prefix"
    alert_days = {
        "ep-dev-a": (DAY_0, DAY_1),
        "ep-dev-b": (DAY_1, DAY_2),
    }
    with create_export_stores(run_dir) as stores:
        for episode_id in alert_days:
            insert_operational_record(
                stores.operational,
                "episodes",
                {"episode_id": episode_id, "role": "development"},
            )
        persist_monitoring_rows(
            stores.operational,
            "daily_metrics",
            [
                _metric_on(episode_id, as_of_time)
                for episode_id in alert_days
                for as_of_time in (DAY_0, DAY_1, DAY_2)
            ],
        )
        persist_monitoring_rows(
            stores.operational,
            "alerts",
            [
                _alert_on(episode_id, as_of_time)
                for episode_id, days in alert_days.items()
                for as_of_time in days
            ],
        )
        database_path = stores.operational_path
    provider, _ = _fake_provider(database_path)

    report = _run_measure(run_dir, provider, run_dir / "prefix.json", count=2)

    assert report["status"] == "complete"
    selected = [
        (entry["episode_id"], entry["as_of_time"]) for entry in report["selected_incidents"]
    ]
    assert selected == [("ep-dev-a", DAY_0), ("ep-dev-b", DAY_1)]
    latest_selected = max(as_of_time for _, as_of_time in selected)
    with sqlite3.connect(database_path) as connection:
        snapshots = connection.execute(
            "SELECT episode_id, available_at, event_at FROM incidents "
            "ORDER BY episode_id, available_at"
        ).fetchall()
    assert [(episode_id, available_at) for episode_id, available_at, _ in snapshots] == [
        ("ep-dev-a", DAY_0),
        ("ep-dev-a", DAY_1),
        ("ep-dev-b", DAY_1),
    ]
    assert all(
        available_at <= latest_selected and event_at <= latest_selected
        for _, available_at, event_at in snapshots
    )
