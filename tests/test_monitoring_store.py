from __future__ import annotations

import csv
import io
import json
import sqlite3

import pytest

from monitoring.incidents import INCIDENT_COLUMNS, incidents_csv
from monitoring.metrics import DAILY_METRIC_COLUMNS, metrics_csv
from monitoring.signals import ALERT_COLUMNS, alerts_csv
from monitoring.store import persist_monitoring_rows
from simulator.asof import read_as_of
from simulator.schema import AS_OF_TABLES
from simulator.storage import StorageError, create_export_stores, insert_operational_record


T0 = "2026-01-31T17:00:00.000000Z"
SNAPSHOT = "2026-02-10T16:59:59.999999Z"
LATER = "2026-02-11T16:59:59.999999Z"


def _metric(episode_id: str, as_of_time: str = SNAPSHOT) -> dict[str, object]:
    row = {
        "metric_id": "psi.amount_avg_7d",
        "episode_id": episode_id,
        "as_of_time": as_of_time,
        "event_at": as_of_time,
        "available_at": as_of_time,
        "business_date": "2026-02-10",
        "feature": "amount_avg_7d",
        "segment": "all",
        "value": 0.3,
        "reference_window": "2025-11-01/2026-01-31",
        "window_start": "2026-02-04",
        "window_end": "2026-02-10",
        "n_window": 30,
        "n_reference": 100,
        "flags": [],
    }
    assert tuple(row) == DAILY_METRIC_COLUMNS
    return row


def _alert(episode_id: str, alert_id: str, as_of_time: str = SNAPSHOT) -> dict[str, object]:
    row = {
        "alert_id": alert_id,
        "episode_id": episode_id,
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
    assert tuple(row) == ALERT_COLUMNS
    return row


def _incident(
    episode_id: str,
    incident_id: str,
    *,
    event_at: str = SNAPSHOT,
    available_at: str = SNAPSHOT,
) -> dict[str, object]:
    row = {
        "incident_id": incident_id,
        "parent_incident_id": None,
        "episode_id": episode_id,
        "alert_ids_json": '["alert-' + episode_id + '"]',
        "feature": "amount_avg_7d",
        "segment": "all",
        "opened_at": SNAPSHOT,
        "status": "open",
        "event_at": event_at,
        "available_at": available_at,
    }
    assert tuple(row) == INCIDENT_COLUMNS
    return row


def _csv_rows(contents: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(contents, newline="")))


def test_monitoring_tables_are_as_of_allowlisted_and_truth_free(tmp_path) -> None:
    store = create_export_stores(tmp_path / "stores")
    try:
        for episode_id in ("ep-a", "ep-b"):
            insert_operational_record(
                store.operational,
                "episodes",
                {"episode_id": episode_id, "role": "development"},
            )
        metric_a, metric_b = _metric("ep-a"), _metric("ep-b")
        alert_a, alert_b = _alert("ep-a", "alert-ep-a"), _alert("ep-b", "alert-ep-b")
        incident_a = _incident("ep-a", "incident-ep-a", available_at=LATER)
        incident_b = _incident("ep-b", "incident-ep-b")
        for table, rows in (
            ("daily_metrics", [metric_a, metric_b]),
            ("alerts", [alert_a, alert_b]),
            ("incidents", [incident_a, incident_b]),
        ):
            assert table in AS_OF_TABLES
            persist_monitoring_rows(store.operational, table, rows)

        schema = {
            table: columns
            for table, columns in store.operational.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'table'"
            )
            if table in {"daily_metrics", "alerts", "incidents"}
        }
        for table in ("daily_metrics", "alerts", "incidents"):
            columns = {
                row[1]
                for row in store.operational.execute(f'PRAGMA table_info("{table}")')
            }
            assert not columns.intersection(
                {"truth", "cause", "template", "seed", "mechanism", "accepted_actions_json"}
            )
            assert "incident_truth" not in schema

        as_of_kwargs = {
            "episode_id": "ep-a",
            "t0": T0,
            "as_of_time": SNAPSHOT,
        }
        assert [row["episode_id"] for row in read_as_of(store.operational, "daily_metrics", **as_of_kwargs)] == ["ep-a"]
        assert [row["episode_id"] for row in read_as_of(store.operational, "alerts", **as_of_kwargs)] == ["ep-a"]
        assert read_as_of(store.operational, "incidents", **as_of_kwargs) == []

        later_rows = read_as_of(
            store.operational,
            "incidents",
            episode_id="ep-a",
            t0=T0,
            as_of_time=LATER,
        )
        assert [row["incident_id"] for row in later_rows] == ["incident-ep-a"]
    finally:
        store.close()


def test_monitoring_persistence_matches_memory_and_csv_and_is_idempotent(tmp_path) -> None:
    store = create_export_stores(tmp_path / "stores")
    try:
        insert_operational_record(
            store.operational,
            "episodes",
            {"episode_id": "ep-a", "role": "development"},
        )
        metric = _metric("ep-a")
        alert = _alert("ep-a", "alert-ep-a")
        incident = _incident("ep-a", "incident-ep-a")
        outputs = {
            "daily_metrics": [metric],
            "alerts": [alert],
            "incidents": [incident],
        }
        assert persist_monitoring_rows(store.operational, "daily_metrics", outputs["daily_metrics"]) == 1
        assert persist_monitoring_rows(store.operational, "alerts", outputs["alerts"]) == 1
        assert persist_monitoring_rows(store.operational, "incidents", outputs["incidents"]) == 1

        csv_by_table = {
            "daily_metrics": _csv_rows(metrics_csv([metric])),
            "alerts": _csv_rows(alerts_csv([alert])),
            "incidents": _csv_rows(incidents_csv([incident])),
        }
        for table, rows in outputs.items():
            db_rows = [
                dict(row)
                for row in store.operational.execute(f'SELECT * FROM "{table}" ORDER BY rowid')
            ]
            assert len(db_rows) == len(rows) == len(csv_by_table[table])
            if table == "daily_metrics":
                for db_row in db_rows:
                    db_row["flags"] = json.loads(db_row["flags"])
                csv_row = csv_by_table[table][0]
                csv_row.update(
                    value=float(csv_row["value"]),
                    n_window=int(csv_row["n_window"]),
                    n_reference=int(csv_row["n_reference"]),
                    flags=json.loads(csv_row["flags"]),
                )
                assert db_rows == rows
                assert db_rows == [csv_row]
            elif table == "alerts":
                for db_row in db_rows:
                    db_row["value"] = float(db_row["value"])
                    db_row["threshold"] = float(db_row["threshold"])
                csv_row = csv_by_table[table][0]
                csv_row["value"] = float(csv_row["value"])
                csv_row["threshold"] = float(csv_row["threshold"])
                assert db_rows == rows
                assert db_rows == [csv_row]
            else:
                csv_row = csv_by_table[table][0]
                if csv_row["parent_incident_id"] == "":
                    csv_row["parent_incident_id"] = None
                assert db_rows == rows
                assert db_rows == [csv_row]

        before = {
            table: store.operational.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
            for table in outputs
        }
        for table, rows in outputs.items():
            assert persist_monitoring_rows(store.operational, table, rows) == 0
        after = {
            table: store.operational.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
            for table in outputs
        }
        assert after == before

        changed = dict(metric, value=0.4)
        with pytest.raises(StorageError, match="different content"):
            persist_monitoring_rows(store.operational, "daily_metrics", [changed])
    finally:
        store.close()
