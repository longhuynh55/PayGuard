from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from model.data import (
    InsufficientSplitSupportError,
    build_splits,
    load_model_config,
    target_for_label_state,
)
from model.features import FEATURE_NAMES, build_feature_splits
from simulator.asof import read_as_of
from simulator.config import load_simulator_config
from simulator.fraud import generate_card_testing
from simulator.labels import generate_label_events, snapshot_labels
from simulator.storage import create_store_pair, insert_operational_record
from simulator.transactions import generate_transactions
from simulator.world import generate_world


ROOT = Path(__file__).resolve().parents[1]
EPISODE_ID = "p2-model-smoke"
T0 = "2026-01-31T17:00:00.000000Z"


@pytest.fixture
def smoke_store(tmp_path: Path):
    simulator_config = load_simulator_config(ROOT / "configs" / "simulator.yaml")
    pair = create_store_pair(
        tmp_path / "operational" / "observations.sqlite",
        tmp_path / "private" / "evaluator.sqlite",
    )
    insert_operational_record(
        pair.operational,
        "episodes",
        {"episode_id": EPISODE_ID, "role": "development"},
    )
    generate_world(pair.operational, simulator_config, "smoke")
    generate_transactions(pair.operational, simulator_config, "smoke")
    generate_card_testing(pair.operational, pair.private, simulator_config)
    generate_label_events(pair.operational, pair.private, simulator_config)
    pair.commit()
    try:
        yield pair, simulator_config
    finally:
        pair.close()


def _date_for_event(event_at: str) -> date:
    timestamp = datetime.fromisoformat(event_at.replace("Z", "+00:00"))
    return timestamp.astimezone(ZoneInfo("Asia/Ho_Chi_Minh")).date()


def _age_for_event(event_at: str) -> timedelta:
    timestamp = datetime.fromisoformat(event_at.replace("Z", "+00:00")).astimezone(UTC)
    snapshot_at = datetime.fromisoformat(T0.replace("Z", "+00:00")).astimezone(UTC)
    return snapshot_at - timestamp


def test_model_splits_are_non_overlapping_mature_and_match_smoke_audit(smoke_store) -> None:
    pair, simulator_config = smoke_store
    config = load_model_config(ROOT / "configs" / "model.yaml")
    splits = build_splits(
        pair.operational,
        simulator_config,
        episode_id=EPISODE_ID,
        model_config=config,
    )

    assert config.provenance == "docs/model.md, section 1."
    assert (config.fit.start_inclusive.isoformat(), config.fit.end_exclusive.isoformat()) == (
        "2025-07-01",
        "2025-09-22",
    )
    assert (
        config.validation.start_inclusive.isoformat(),
        config.validation.end_exclusive.isoformat(),
    ) == ("2025-09-22", "2025-10-01")
    assert (
        config.calibration.start_inclusive.isoformat(),
        config.calibration.end_exclusive.isoformat(),
    ) == ("2025-10-01", "2025-11-01")

    result_rows = {
        "fit": splits.fit,
        "validation": splits.validation,
        "calibration": splits.calibration,
    }
    id_sets = {name: {row.txn_id for row in rows} for name, rows in result_rows.items()}
    assert id_sets["fit"].isdisjoint(id_sets["validation"])
    assert id_sets["fit"].isdisjoint(id_sets["calibration"])
    assert id_sets["validation"].isdisjoint(id_sets["calibration"])

    for name, rows in result_rows.items():
        window = getattr(config, name)
        for row in rows:
            business_date = _date_for_event(row.event_at)
            assert window.start_inclusive <= business_date < window.end_exclusive
            assert _age_for_event(row.event_at) >= timedelta(
                days=simulator_config.label_reporting.maturity_horizon_days.value
            )
            assert row.label_state != "pending"
            assert row.target == (1 if row.label_state == "confirmed_fraud" else 0)

    expected_targets = {
        "confirmed_fraud": 1,
        "confirmed_legit": 0,
        "matured_negative": 0,
    }
    transactions = read_as_of(
        pair.operational,
        "transactions",
        episode_id=EPISODE_ID,
        t0=T0,
        as_of_time=T0,
    )
    snapshots = snapshot_labels(
        pair.operational,
        simulator_config,
        episode_id=EPISODE_ID,
        as_of_time=T0,
    )
    snapshots_by_id = {snapshot.txn_id: snapshot for snapshot in snapshots}
    for name, rows in result_rows.items():
        window = getattr(config, name)
        candidates = [
            transaction
            for transaction in transactions
            if window.start_inclusive <= _date_for_event(transaction["event_at"]) < window.end_exclusive
        ]
        expected = [
            (transaction, snapshots_by_id[transaction["txn_id"]])
            for transaction in candidates
            if _age_for_event(transaction["event_at"])
            >= timedelta(days=simulator_config.label_reporting.maturity_horizon_days.value)
            and snapshots_by_id[transaction["txn_id"]].state in expected_targets
        ]
        audit = splits.audit.for_split(name)
        assert audit.candidate_count == len(candidates)
        assert audit.age_eligible_count == sum(
            _age_for_event(transaction["event_at"])
            >= timedelta(days=simulator_config.label_reporting.maturity_horizon_days.value)
            for transaction in candidates
        )
        assert audit.under_horizon_excluded_count == sum(
            _age_for_event(transaction["event_at"])
            < timedelta(days=simulator_config.label_reporting.maturity_horizon_days.value)
            for transaction in candidates
        )
        assert audit.pending_excluded_count == sum(
            snapshots_by_id[transaction["txn_id"]].state == "pending"
            for transaction in candidates
        )
        assert audit.sample_count == len(expected) == len(rows)
        assert audit.positive_count == sum(
            snapshots_by_id[transaction["txn_id"]].state == "confirmed_fraud"
            for transaction, _ in expected
        )
        assert audit.negative_count == audit.sample_count - audit.positive_count
        assert audit.positive_count > 0
        assert audit.negative_count > 0

    assert "fit [2025-07-01, 2025-09-22): n=" in splits.audit.format()
    assert "validation [2025-09-22, 2025-10-01): n=" in splits.audit.format()
    assert "calibration [2025-10-01, 2025-11-01): n=" in splits.audit.format()


def test_smoke_split_to_feature_matrices_is_asof_and_deterministic(smoke_store) -> None:
    pair, simulator_config = smoke_store
    splits = build_splits(pair.operational, simulator_config, episode_id=EPISODE_ID)
    before = build_feature_splits(pair.operational, splits)

    split_examples = {
        "fit": splits.fit,
        "validation": splits.validation,
        "calibration": splits.calibration,
    }
    matrices = {
        "fit": before.fit,
        "validation": before.validation,
        "calibration": before.calibration,
    }
    for name, examples in split_examples.items():
        matrix = matrices[name]
        assert matrix.examples == examples
        assert len(matrix.values) == len(examples)
        assert matrix.feature_names == FEATURE_NAMES
        assert all(len(row) == len(FEATURE_NAMES) for row in matrix.values)

    latest_name, latest_example = max(
        (
            (name, example)
            for name, examples in split_examples.items()
            for example in examples
        ),
        key=lambda item: (item[1].event_at, item[1].txn_id),
    )
    source_transaction = next(
        row
        for row in read_as_of(
            pair.operational,
            "transactions",
            episode_id=EPISODE_ID,
            t0=T0,
            as_of_time=T0,
        )
        if row["txn_id"] == latest_example.txn_id
    )
    decision_at = datetime.fromisoformat(latest_example.event_at.replace("Z", "+00:00"))
    late_created_at = decision_at - timedelta(minutes=1)
    late_available_at = decision_at + timedelta(seconds=1)
    late_txn_id = "p2-smoke-late-arrival"
    insert_operational_record(
        pair.operational,
        "transactions",
        {
            "txn_id": late_txn_id,
            "episode_id": source_transaction["episode_id"],
            "customer_id": source_transaction["customer_id"],
            "merchant_id": source_transaction["merchant_id"],
            "created_at": late_created_at,
            "event_at": late_created_at,
            "available_at": late_available_at,
            "amount": 999999,
            "channel": "qr",
        },
    )
    pair.operational.commit()

    available_transactions = read_as_of(
        pair.operational,
        "transactions",
        episode_id=EPISODE_ID,
        t0=T0,
        as_of_time=T0,
    )
    assert any(row["txn_id"] == late_txn_id for row in available_transactions)

    after = build_feature_splits(pair.operational, splits)
    target_index = matrices[latest_name].txn_ids.index(latest_example.txn_id)
    after_matrix = getattr(after, latest_name)
    assert after_matrix.values[target_index] == matrices[latest_name].values[target_index]
    assert after == before
    assert build_feature_splits(pair.operational, splits) == after


def test_pending_state_is_excluded_and_never_mapped_to_zero(smoke_store) -> None:
    pair, simulator_config = smoke_store
    pending_txn_id = "txn-base-p2-pending-control"
    insert_operational_record(
        pair.operational,
        "transactions",
        {
            "txn_id": pending_txn_id,
            "episode_id": "base",
            "customer_id": "customer-000001",
            "merchant_id": "merchant-000001",
            "created_at": "2025-12-01T03:00:00Z",
            "event_at": "2025-12-01T03:00:00Z",
            "available_at": "2025-12-01T03:00:00Z",
            "amount": 15000,
            "channel": "qr",
        },
    )
    pair.operational.commit()

    snapshot = next(
        row
        for row in snapshot_labels(
            pair.operational,
            simulator_config,
            episode_id=EPISODE_ID,
            as_of_time=T0,
        )
        if row.txn_id == pending_txn_id
    )
    assert snapshot.state == "pending"
    assert target_for_label_state("pending") is None
    assert target_for_label_state("confirmed_fraud") == 1
    assert target_for_label_state("confirmed_legit") == 0
    assert target_for_label_state("matured_negative") == 0

    splits = build_splits(pair.operational, simulator_config, episode_id=EPISODE_ID)
    all_ids = {
        row.txn_id
        for rows in (splits.fit, splits.validation, splits.calibration)
        for row in rows
    }
    assert pending_txn_id not in all_ids


def test_model_reader_uses_only_the_operational_connection(smoke_store, monkeypatch) -> None:
    pair, simulator_config = smoke_store
    operational_path = Path(
        pair.operational.execute("PRAGMA database_list").fetchone()[2]
    ).resolve()
    assert operational_path == pair.operational_path

    def reject_new_connection(*args, **kwargs):
        raise AssertionError("model split reader must not open another database")

    monkeypatch.setattr(sqlite3, "connect", reject_new_connection)
    statements: list[str] = []
    pair.operational.set_trace_callback(statements.append)
    try:
        splits = build_splits(pair.operational, simulator_config, episode_id=EPISODE_ID)
    finally:
        pair.operational.set_trace_callback(None)

    assert splits.audit.splits
    assert statements
    assert all("private" not in statement.lower() for statement in statements)
    assert all("truth" not in statement.lower() for statement in statements)
    assert any('FROM "transactions"' in statement for statement in statements)
    assert any('FROM "label_events"' in statement for statement in statements)


def test_missing_class_support_raises_with_audit_report(tmp_path: Path) -> None:
    simulator_config = load_simulator_config(ROOT / "configs" / "simulator.yaml")
    pair = create_store_pair(
        tmp_path / "operational" / "observations.sqlite",
        tmp_path / "private" / "evaluator.sqlite",
    )
    insert_operational_record(
        pair.operational,
        "episodes",
        {"episode_id": EPISODE_ID, "role": "development"},
    )
    pair.commit()
    try:
        with pytest.raises(InsufficientSplitSupportError) as raised:
            build_splits(pair.operational, simulator_config, episode_id=EPISODE_ID)
    finally:
        pair.close()

    error = raised.value
    assert "minimum 1 example for each of classes 0 and 1" in str(error)
    assert "fit: missing positive class support" in str(error)
    assert "fit: missing negative class support" in str(error)
    assert "calibration: missing positive class support" in str(error)
    assert all(item.sample_count == 0 for item in error.report.splits)
    assert all(item.positive_count == 0 for item in error.report.splits)

