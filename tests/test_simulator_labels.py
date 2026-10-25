from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

from simulator.asof import read_as_of
from simulator.config import SimulatorConfig, validate_simulator_config
from simulator.fraud import CARD_TESTING_METADATA_KEY, generate_card_testing
from simulator.labels import (
    LABEL_TRUTH_METADATA_KEY,
    generate_label_events,
    label_delay_report,
    snapshot_labels,
)
from simulator.reproducibility import canonical_content
from simulator.storage import (
    create_export_stores,
    insert_operational_record,
    list_operational_schema,
)
from simulator.transactions import generate_transactions
from simulator.world import generate_world


ROOT = Path(__file__).resolve().parents[1]
T0 = "2026-01-31T17:00:00Z"


def _config(**label_values: object) -> SimulatorConfig:
    raw = yaml.safe_load((ROOT / "configs" / "simulator.yaml").read_text(encoding="utf-8"))
    for name, value in label_values.items():
        raw["label_reporting"][name]["value"] = value
    return validate_simulator_config(raw)


def _generated_pair(tmp_path: Path, config: SimulatorConfig):
    pair = create_export_stores(tmp_path / "label-run")
    pair.operational.execute(
        "INSERT INTO episodes (episode_id, role) VALUES ('ep-a', 'development')"
    )
    generate_world(pair.operational, config, "smoke")
    generate_transactions(pair.operational, config, "smoke")
    generate_card_testing(pair.operational, pair.private, config)
    before_transactions = canonical_content(
        pair.operational, tables=("transactions", "transaction_events")
    )
    summary = generate_label_events(pair.operational, pair.private, config)
    after_transactions = canonical_content(
        pair.operational, tables=("transactions", "transaction_events")
    )
    pair.commit()
    return pair, summary, before_transactions, after_transactions


def _snapshot_by_id(pair, config: SimulatorConfig, at: datetime | str) -> dict[str, dict]:
    return {
        item.txn_id: {
            "state": item.state,
            "label": item.label,
            "label_event_id": item.label_event_id,
        }
        for item in snapshot_labels(
            pair.operational, config, episode_id="ep-a", as_of_time=at
        )
    }


def test_label_generator_uses_private_truth_and_leaves_transaction_features_unchanged(
    tmp_path: Path,
) -> None:
    config = _config()
    pair, summary, before_transactions, after_transactions = _generated_pair(tmp_path, config)
    try:
        assert before_transactions == after_transactions
        transactions = {
            row["txn_id"]: row
            for row in pair.operational.execute("SELECT txn_id, channel FROM transactions")
        }
        labels = [dict(row) for row in pair.operational.execute("SELECT * FROM label_events")]
        assert len(labels) == summary.label_event_count
        assert len({row["txn_id"] for row in labels}) == len(labels)
        assert summary.transaction_count == len(transactions)
        assert summary.label_tail_end_at == (
            datetime.fromisoformat(summary.last_transaction_at.replace("Z", "+00:00"))
            + timedelta(days=max(90, config.label_reporting.maximum_delay_days.value))
        ).isoformat(timespec="microseconds").replace("+00:00", "Z")
        assert summary.label_tail_end_at > summary.last_transaction_at

        for row in labels:
            channel = transactions[row["txn_id"]]["channel"]
            expected_source = "chargeback" if channel == "card" else "customer_report"
            assert row["source"] == expected_source
            delay = datetime.fromisoformat(row["reported_at"].replace("Z", "+00:00")) - datetime.fromisoformat(
                row["event_at"].replace("Z", "+00:00")
            )
            assert timedelta(days=config.label_reporting.minimum_delay_days.value) <= delay
            assert delay <= timedelta(days=config.label_reporting.maximum_delay_days.value)
            assert delay.total_seconds() % 86400 == 0

        truth_row = pair.private.execute(
            "SELECT value_json FROM run_metadata WHERE key = ?", (LABEL_TRUTH_METADATA_KEY,)
        ).fetchone()
        truth = json.loads(truth_row[0])
        unreported_fraud = {
            item["txn_id"]
            for item in truth["transactions"]
            if item["true_label"] == "fraud" and not item["reported"]
        }
        assert unreported_fraud
        assert not unreported_fraud & {row["txn_id"] for row in labels}
        assert pair.operational.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (LABEL_TRUTH_METADATA_KEY,),
        ).fetchone() is None
        assert not any(
            "truth" in name.lower() or "truth" in column.lower()
            for name, columns in list_operational_schema(pair.operational).items()
            for column in columns
        )
    finally:
        pair.close()


def test_zero_reporting_keeps_unreported_fraud_only_in_private_truth(tmp_path: Path) -> None:
    config = _config(probability_reported=0.0)
    pair, summary, _, _ = _generated_pair(tmp_path, config)
    try:
        assert summary.label_event_count == 0
        assert pair.operational.execute("SELECT COUNT(*) FROM label_events").fetchone()[0] == 0
        truth_row = pair.private.execute(
            "SELECT value_json FROM run_metadata WHERE key = ?", (LABEL_TRUTH_METADATA_KEY,)
        ).fetchone()
        truth = json.loads(truth_row[0])
        fraud = [item for item in truth["transactions"] if item["true_label"] == "fraud"]
        assert fraud
        assert all(not item["reported"] for item in fraud)
        labeled_txn_ids = {
            row[0] for row in pair.operational.execute("SELECT txn_id FROM label_events")
        }
        assert not labeled_txn_ids
        assert not {item["txn_id"] for item in fraud} & labeled_txn_ids

        report = label_delay_report(
            pair.operational,
            pair.private,
            config,
            episode_id="ep-a",
            as_of_time=summary.label_tail_end_at,
        )
        assert report["oracle"]["true_fraud_count"] == len(fraud)
        assert report["oracle"]["fraud_never_reported_count"] == len(fraud)
        assert report["observed"]["snapshot_state_counts"]["matured_negative"] == summary.transaction_count
        assert report["observed"]["censoring"][
            "policy_horizon_censored_matured_negative_count"
        ] == summary.transaction_count
        assert report["oracle"]["label_tail_complete_at_snapshot"] is True
    finally:
        pair.close()


def test_label_report_after_horizon_is_oracle_visible_but_not_in_matured_snapshot(
    tmp_path: Path,
) -> None:
    config = _config(
        probability_reported=1.0,
        minimum_delay_days=120,
        maximum_delay_days=120,
    )
    pair, summary, _, _ = _generated_pair(tmp_path, config)
    try:
        truth_row = pair.private.execute(
            "SELECT value_json FROM run_metadata WHERE key = ?", (LABEL_TRUTH_METADATA_KEY,)
        ).fetchone()
        truth = json.loads(truth_row[0])
        fraud_outcome = next(
            item for item in truth["transactions"] if item["true_label"] == "fraud"
        )
        transaction_at = datetime.fromisoformat(fraud_outcome["event_at"].replace("Z", "+00:00"))
        at_horizon = transaction_at + timedelta(days=90)
        after_report = transaction_at + timedelta(days=120)

        assert _snapshot_by_id(pair, config, at_horizon)[fraud_outcome["txn_id"]]["state"] == "matured_negative"
        assert _snapshot_by_id(pair, config, after_report)[fraud_outcome["txn_id"]]["state"] == "confirmed_fraud"
        report = label_delay_report(
            pair.operational,
            pair.private,
            config,
            episode_id="ep-a",
            as_of_time=summary.label_tail_end_at,
        )
        assert report["oracle"]["fraud_reported_after_horizon_count"] > 0
        assert report["observed"]["snapshot_state_counts"]["confirmed_fraud"] > 0
        assert report["observed"]["available_label_delay_days"]["minimum"] == 120
        assert report["observed"]["available_label_delay_days"]["maximum"] == 120
    finally:
        pair.close()


def test_snapshot_maturity_boundary_report_availability_and_historical_stability(
    p1_2_store,
) -> None:
    config = _config()
    before_time = "2026-01-31T17:00:00Z"
    before_snapshot = _snapshot_by_id(p1_2_store, config, before_time)
    before_features = read_as_of(
        p1_2_store.operational,
        "transactions",
        episode_id="ep-a",
        t0=T0,
        as_of_time=before_time,
    )
    txn_id = "txn-base-at-boundary"
    assert before_snapshot[txn_id]["state"] == "pending"
    insert_operational_record(
        p1_2_store.operational,
        "label_events",
        {
            "event_id": "label-reported-before-available",
            "txn_id": txn_id,
            "episode_id": "base",
            "event_at": "2026-01-31T16:59:59.000000Z",
            "label": "fraud",
            "label_status": "confirmed",
            "reported_at": "2026-01-31T17:00:00Z",
            "available_at": "2026-02-02T00:00:00Z",
            "source": "chargeback",
            "supersedes_event_id": None,
        },
    )
    p1_2_store.operational.commit()
    stored_label = p1_2_store.operational.execute(
        "SELECT reported_at, available_at FROM label_events WHERE event_id = ?",
        ("label-reported-before-available",),
    ).fetchone()
    assert stored_label["reported_at"] <= before_time < stored_label["available_at"]
    assert not any(
        row["event_id"] == "label-reported-before-available"
        for row in read_as_of(
            p1_2_store.operational,
            "label_events",
            episode_id="ep-a",
            t0=T0,
            as_of_time=before_time,
        )
    )

    after_insert = _snapshot_by_id(p1_2_store, config, before_time)
    assert after_insert == before_snapshot
    after_features = read_as_of(
        p1_2_store.operational,
        "transactions",
        episode_id="ep-a",
        t0=T0,
        as_of_time=before_time,
    )
    assert after_features == before_features
    assert _snapshot_by_id(p1_2_store, config, "2026-02-02T00:00:00Z")[txn_id]["state"] == "confirmed_fraud"
    assert _snapshot_by_id(p1_2_store, config, "2026-02-02T01:00:00Z")[
        "txn-base-before-t0"
    ]["state"] == "confirmed_legit"

    boundary_txn = "txn-a-not-yet-available"
    event_at = datetime.fromisoformat("2026-02-01T00:01:00+00:00")
    just_before_h = event_at + timedelta(days=90) - timedelta(microseconds=1)
    at_h = event_at + timedelta(days=90)
    assert _snapshot_by_id(p1_2_store, config, just_before_h)[boundary_txn]["state"] == "pending"
    assert _snapshot_by_id(p1_2_store, config, at_h)[boundary_txn]["state"] == "matured_negative"


def test_late_label_does_not_rewrite_matured_negative_and_only_one_conclusion_allowed(
    p1_2_store,
) -> None:
    config = _config()
    txn_id = "txn-a-not-yet-available"
    event_at = datetime.fromisoformat("2026-02-01T00:01:00+00:00")
    at_h = event_at + timedelta(days=90)
    after_h = event_at + timedelta(days=120)
    insert_operational_record(
        p1_2_store.operational,
        "label_events",
        {
            "event_id": "label-fraud-after-horizon",
            "txn_id": txn_id,
            "episode_id": "ep-a",
            "event_at": event_at,
            "label": "fraud",
            "label_status": "confirmed",
            "reported_at": after_h,
            "available_at": after_h,
            "source": "customer_report",
            "supersedes_event_id": None,
        },
    )
    p1_2_store.operational.commit()
    assert _snapshot_by_id(p1_2_store, config, at_h)[txn_id]["state"] == "matured_negative"
    assert _snapshot_by_id(p1_2_store, config, after_h)[txn_id]["state"] == "confirmed_fraud"

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        insert_operational_record(
            p1_2_store.operational,
            "label_events",
            {
                "event_id": "second-label-same-transaction",
                "txn_id": txn_id,
                "episode_id": "ep-a",
                "event_at": event_at,
                "label": "legit",
                "label_status": "confirmed",
                "reported_at": after_h,
                "available_at": after_h,
                "source": "customer_report",
                "supersedes_event_id": None,
            },
        )
