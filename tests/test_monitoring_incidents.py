from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3

import pytest

from monitoring.__main__ import main
from monitoring.incidents import (
    InMemoryDecisionSource,
    MonitoringIncidentsError,
    compute_incidents,
)
from monitoring.signals import ALERT_COLUMNS, alerts_csv, compute_alerts
from simulator.storage import create_export_stores, insert_operational_record


_BASE = datetime(2026, 2, 1, 12, 0, tzinfo=UTC)


def _timestamp(offset: timedelta = timedelta(0)) -> str:
    return (_BASE + offset).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _alert(
    alert_id: str,
    offset: timedelta = timedelta(0),
    *,
    episode_id: str = "ep-a",
    feature: str = "amount_avg_7d",
    segment: str = "all",
    available_offset: timedelta | None = None,
) -> dict[str, object]:
    as_of_time = _timestamp(offset)
    available_offset = offset if available_offset is None else available_offset
    row = {
        "alert_id": alert_id,
        "episode_id": episode_id,
        "as_of_time": as_of_time,
        "metric": f"psi.{feature}",
        "feature": feature,
        "segment": segment,
        "value": 0.3,
        "reference": "2025-11-01/2026-01-31",
        "threshold": 0.25,
        "event_at": as_of_time,
        "available_at": _timestamp(available_offset),
    }
    assert set(row) == set(ALERT_COLUMNS)
    return row


def _decision(
    decision_id: str,
    incident_id: str,
    offset: timedelta,
    action: str,
    *,
    episode_id: str = "ep-a",
) -> dict[str, str]:
    return {
        "decision_id": decision_id,
        "incident_id": incident_id,
        "episode_id": episode_id,
        "decision_at": _timestamp(offset),
        "action": action,
    }


def _latest_rows(result) -> dict[str, dict[str, object]]:
    latest: dict[str, dict[str, object]] = {}
    for row in result.incidents:
        latest[row["incident_id"]] = row
    return latest


def _alert_ids(row: dict[str, object]) -> list[str]:
    return json.loads(str(row["alert_ids_json"]))


def _single_incident(result) -> dict[str, object]:
    latest = _latest_rows(result)
    assert len(latest) == 1
    return next(iter(latest.values()))


def test_open_incident_merges_same_feature_segment_at_exact_two_day_boundary() -> None:
    first = _alert("alert-a")
    second = _alert("alert-b", timedelta(days=2))

    result = compute_incidents([second, first], episode_id="ep-a")

    incident = _single_incident(result)
    assert _alert_ids(incident) == ["alert-a", "alert-b"]
    assert incident["opened_at"] == first["as_of_time"]
    assert result.report["incident_count"] == 1


def test_open_merge_window_slides_from_the_most_recent_alert() -> None:
    result = compute_incidents(
        [
            _alert("alert-a"),
            _alert("alert-b", timedelta(days=2)),
            _alert("alert-c", timedelta(days=4)),
        ],
        episode_id="ep-a",
    )

    incident = _single_incident(result)
    assert _alert_ids(incident) == ["alert-a", "alert-b", "alert-c"]


def test_open_incident_more_than_two_days_after_latest_alert_starts_new_incident() -> None:
    result = compute_incidents(
        [
            _alert("alert-a"),
            _alert("alert-b", timedelta(days=2, microseconds=1)),
        ],
        episode_id="ep-a",
    )

    latest = _latest_rows(result)
    assert len(latest) == 2
    assert all(row["parent_incident_id"] is None for row in latest.values())
    assert result.report["incident_count"] == 2


@pytest.mark.parametrize(
    ("second_feature", "second_segment"),
    [("amount_avg_7d", "model_version=m2"), ("txn_count_24h", "all")],
)
def test_different_feature_or_segment_does_not_merge(
    second_feature: str, second_segment: str
) -> None:
    result = compute_incidents(
        [
            _alert("alert-a"),
            _alert(
                "alert-b",
                timedelta(hours=1),
                feature=second_feature,
                segment=second_segment,
            ),
        ],
        episode_id="ep-a",
    )

    latest = _latest_rows(result)
    assert len(latest) == 2
    assert all(row["parent_incident_id"] is None for row in latest.values())
    assert {_alert_ids(row)[0] for row in latest.values()} == {"alert-a", "alert-b"}


def test_recheck_keeps_incident_open_for_later_alerts() -> None:
    first = _alert("alert-a")
    second = _alert("alert-b", timedelta(days=1))
    incident_id = _single_incident(
        compute_incidents([first], episode_id="ep-a")
    )["incident_id"]
    decisions = InMemoryDecisionSource(
        [_decision("decision-recheck", str(incident_id), timedelta(hours=12), "recheck")]
    )

    result = compute_incidents(
        [first, second], episode_id="ep-a", decision_source=decisions
    )

    incident = _single_incident(result)
    assert incident["status"] == "recheck"
    assert _alert_ids(incident) == ["alert-a", "alert-b"]
    assert result.report["child_incident_count"] == 0


def test_final_decision_creates_child_within_two_days_and_preserves_parent() -> None:
    first = _alert("alert-a")
    latest_parent_alert = _alert("alert-b", timedelta(days=1))
    child_alert = _alert("alert-c", timedelta(days=3))
    parent_id = str(
        _single_incident(
            compute_incidents([first, latest_parent_alert], episode_id="ep-a")
        )["incident_id"]
    )
    source_rows = [
        _decision("decision-close", parent_id, timedelta(days=1, hours=12), "close"),
        _decision("decision-later", parent_id, timedelta(days=2), "fraud_ops"),
    ]
    decision_source = InMemoryDecisionSource(source_rows)
    before = tuple(dict(row) for row in decision_source.decisions)

    result = compute_incidents(
        [child_alert, latest_parent_alert, first],
        episode_id="ep-a",
        decision_source=decision_source,
    )

    latest = _latest_rows(result)
    parent = latest[parent_id]
    children = [row for row in latest.values() if row["parent_incident_id"] == parent_id]
    assert parent["status"] == "close"
    assert _alert_ids(parent) == ["alert-a", "alert-b"]
    assert parent["event_at"] == _timestamp(timedelta(days=1, hours=12))
    assert parent["available_at"] == parent["event_at"]
    assert len(children) == 1
    assert children[0]["status"] == "open"
    assert _alert_ids(children[0]) == ["alert-c"]
    assert children[0]["event_at"] == child_alert["as_of_time"]
    assert tuple(dict(row) for row in decision_source.decisions) == before
    assert result.report["incident_count"] == 2


def test_alert_after_final_decision_more_than_two_days_from_last_alert_is_independent() -> None:
    first = _alert("alert-a")
    latest_parent_alert = _alert("alert-b", timedelta(days=1))
    later = _alert("alert-c", timedelta(days=3, microseconds=1))
    parent_id = str(
        _single_incident(
            compute_incidents([first, latest_parent_alert], episode_id="ep-a")
        )["incident_id"]
    )
    source = InMemoryDecisionSource(
        [_decision("decision-close", parent_id, timedelta(days=1, hours=12), "close")]
    )

    result = compute_incidents(
        [first, latest_parent_alert, later], episode_id="ep-a", decision_source=source
    )

    latest = _latest_rows(result)
    assert latest[parent_id]["status"] == "close"
    independent = [row for key, row in latest.items() if key != parent_id]
    assert len(independent) == 1
    assert independent[0]["parent_incident_id"] is None
    assert _alert_ids(independent[0]) == ["alert-c"]


def test_alerts_before_decision_at_stay_on_parent_and_later_alert_becomes_child() -> None:
    alerts = [
        _alert("alert-a"),
        _alert("alert-b", timedelta(days=1)),
        _alert("alert-c", timedelta(days=2)),
    ]
    parent_id = str(
        _single_incident(compute_incidents(alerts[:2], episode_id="ep-a"))["incident_id"]
    )
    source = InMemoryDecisionSource(
        [_decision("decision-final", parent_id, timedelta(days=1, hours=12), "fraud_ops")]
    )

    result = compute_incidents(alerts, episode_id="ep-a", decision_source=source)

    latest = _latest_rows(result)
    parent = latest[parent_id]
    children = [row for row in latest.values() if row["parent_incident_id"] == parent_id]
    assert _alert_ids(parent) == ["alert-a", "alert-b"]
    assert parent["status"] == "fraud_ops"
    assert len(children) == 1
    assert _alert_ids(children[0]) == ["alert-c"]


def test_incident_result_is_deterministic_for_reordered_inputs() -> None:
    alerts = [
        _alert("alert-z", timedelta(hours=1)),
        _alert("alert-b"),
        _alert("alert-a", timedelta(hours=1)),
    ]
    parent_id = str(_single_incident(compute_incidents([alerts[1]], episode_id="ep-a"))["incident_id"])
    decision_rows = [_decision("decision-recheck", parent_id, timedelta(minutes=30), "recheck")]

    forward = compute_incidents(
        alerts,
        episode_id="ep-a",
        decision_source=InMemoryDecisionSource(decision_rows),
    )
    reverse = compute_incidents(
        list(reversed(alerts)),
        episode_id="ep-a",
        decision_source=InMemoryDecisionSource(list(reversed(decision_rows))),
    )

    assert forward == reverse


def _daily_metric(
    episode_id: str, offset_days: int, *, feature: str = "amount_avg_7d"
) -> dict[str, object]:
    as_of = _timestamp(timedelta(days=offset_days))
    business_date = (_BASE.date() + timedelta(days=offset_days)).isoformat()
    return {
        "metric_id": f"psi.{feature}",
        "episode_id": episode_id,
        "as_of_time": as_of,
        "event_at": as_of,
        "available_at": as_of,
        "business_date": business_date,
        "feature": feature,
        "segment": "all",
        "value": 0.3,
        "reference_window": "2025-11-01/2026-01-31",
        "window_start": business_date,
        "window_end": business_date,
        "n_window": 30,
        "n_reference": 100,
        "flags": [],
    }


def test_smoke_p32_alert_counts_group_to_incidents_per_episode() -> None:
    expected_alert_counts = {
        "ep-0001": 1,
        "ep-0002": 1,
        "ep-0003": 5,
        "ep-0004": 1,
        "ep-0005": 5,
    }
    alerts_by_episode = {}
    incident_counts = {}
    for episode_id, count in expected_alert_counts.items():
        metrics = [_daily_metric(episode_id, day) for day in range(count)]
        cutoff = max(row["as_of_time"] for row in metrics)
        signals = compute_alerts(
            metrics,
            episode_id=episode_id,
            as_of_time=cutoff,
            profile="smoke",
        )
        alerts_by_episode[episode_id] = signals.alerts
        result = compute_incidents(signals.alerts, episode_id=episode_id)
        incident_counts[episode_id] = result.report["incident_count"]

    assert {episode: len(rows) for episode, rows in alerts_by_episode.items()} == expected_alert_counts
    assert incident_counts == {
        "ep-0001": 1,
        "ep-0002": 1,
        "ep-0003": 1,
        "ep-0004": 1,
        "ep-0005": 1,
    }
    assert incident_counts["ep-0003"] < expected_alert_counts["ep-0003"]
    assert incident_counts["ep-0005"] < expected_alert_counts["ep-0005"]


def test_incidents_cli_reads_alerts_csv_and_writes_output(tmp_path: Path, capsys) -> None:
    alerts_path = tmp_path / "alerts.csv"
    alerts_path.write_text(alerts_csv([_alert("alert-a")]), encoding="utf-8")
    output_path = tmp_path / "incidents.csv"
    store = create_export_stores(tmp_path / "monitoring-store")
    insert_operational_record(
        store.operational,
        "episodes",
        {"episode_id": "ep-a", "role": "development"},
    )
    insert_operational_record(
        store.operational,
        "daily_metrics",
        _daily_metric("ep-a", 0),
    )
    store.commit()
    operational_path = store.operational_path
    store.close()

    exit_code = main(
        [
            "incidents",
            "--operational-db",
            str(operational_path),
            "--episode-id",
            "ep-a",
            "--alerts-csv",
            str(alerts_path),
            "--output",
            str(output_path),
        ]
    )

    assert exit_code == 0
    assert "incident_count=1" in capsys.readouterr().err
    with output_path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 1
    assert rows[0]["status"] == "open"
    assert rows[0]["alert_ids_json"] == '["alert-a"]'

    with sqlite3.connect(operational_path) as connection:
        before = {
            table: connection.execute(f'SELECT * FROM "{table}" ORDER BY 1').fetchall()
            for table in ("alerts", "incidents")
        }
    assert len(before["alerts"]) == 1
    assert len(before["incidents"]) == 1

    repeated = main(
        [
            "incidents",
            "--operational-db",
            str(operational_path),
            "--episode-id",
            "ep-a",
            "--alerts-csv",
            str(alerts_path),
            "--output",
            str(output_path),
        ]
    )
    assert repeated == 0
    with sqlite3.connect(operational_path) as connection:
        after = {
            table: connection.execute(f'SELECT * FROM "{table}" ORDER BY 1').fetchall()
            for table in ("alerts", "incidents")
        }
    assert after == before


def test_decisions_for_another_episode_are_rejected() -> None:
    class WrongEpisodeSource:
        def decisions_for_episode(self, episode_id: str):
            return [
                _decision(
                    "decision",
                    "incident-x",
                    timedelta(hours=1),
                    "close",
                    episode_id="ep-b",
                )
            ]

    with pytest.raises(MonitoringIncidentsError, match="expected 'ep-a'"):
        compute_incidents(
            [_alert("alert-a")],
            episode_id="ep-a",
            decision_source=WrongEpisodeSource(),
        )
