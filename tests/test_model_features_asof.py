from __future__ import annotations

from pathlib import Path

import pytest

import model.features as feature_module
from model.data import ModelExample, ModelSplits, SplitAuditReport
from model.features import FEATURE_NAMES, build_feature_splits
from simulator.storage import create_store_pair, insert_operational_record


EPISODE_ID = "p2-feature-test"
AS_OF_TIME = "2026-01-31T17:00:00.000000Z"
CUSTOMER_START = "2026-01-01T00:00:00.000000Z"
EARLY_TXN = "txn-early"
TARGET_TXN = "txn-target"


@pytest.fixture
def feature_store(tmp_path: Path):
    pair = create_store_pair(
        tmp_path / "operational" / "observations.sqlite",
        tmp_path / "private" / "evaluator.sqlite",
    )
    insert_operational_record(
        pair.operational,
        "episodes",
        {"episode_id": EPISODE_ID, "role": "development"},
    )
    insert_operational_record(
        pair.operational,
        "customers",
        {
            "customer_id": "customer-1",
            "episode_id": "base",
            "event_at": CUSTOMER_START,
            "available_at": CUSTOMER_START,
            "segment": "retail",
            "province": "Hanoi",
            "age_band": "30-39",
        },
    )
    insert_operational_record(
        pair.operational,
        "merchants",
        {
            "merchant_id": "merchant-1",
            "episode_id": "base",
            "event_at": CUSTOMER_START,
            "available_at": CUSTOMER_START,
            "mcc": "5411",
            "province": "Hanoi",
        },
    )
    for txn_id, created_at, available_at, amount in (
        (EARLY_TXN, "2026-01-01T00:30:00Z", "2026-01-01T00:30:00Z", 100),
        ("txn-older", "2026-01-08T10:00:00Z", "2026-01-08T10:00:00Z", 100),
        ("txn-recent", "2026-01-08T11:30:00Z", "2026-01-08T11:30:00Z", 300),
        (TARGET_TXN, "2026-01-08T12:00:00Z", "2026-01-08T12:00:00Z", 500),
    ):
        insert_operational_record(
            pair.operational,
            "transactions",
            {
                "txn_id": txn_id,
                "episode_id": "base",
                "customer_id": "customer-1",
                "merchant_id": "merchant-1",
                "created_at": created_at,
                "event_at": created_at,
                "available_at": available_at,
                "amount": amount,
                "channel": "qr",
            },
        )
    pair.operational.commit()
    try:
        yield pair
    finally:
        pair.close()


def _splits() -> ModelSplits:
    examples = (
        ModelExample(
            txn_id=EARLY_TXN,
            episode_id="base",
            event_at="2026-01-01T00:30:00.000000Z",
            label_state="confirmed_legit",
            target=0,
        ),
        ModelExample(
            txn_id=TARGET_TXN,
            episode_id="base",
            event_at="2026-01-08T12:00:00.000000Z",
            label_state="confirmed_fraud",
            target=1,
        ),
    )
    return ModelSplits(
        episode_id=EPISODE_ID,
        as_of_time=AS_OF_TIME,
        label_policy_version="test-policy",
        fit=examples,
        validation=(),
        calibration=(),
        audit=SplitAuditReport(
            episode_id=EPISODE_ID,
            as_of_time=AS_OF_TIME,
            horizon_days=90,
            label_policy_version="test-policy",
            splits=(),
        ),
    )


def _features_by_id(feature_splits, txn_id: str) -> dict[str, int | float | None]:
    index = feature_splits.fit.txn_ids.index(txn_id)
    return dict(zip(feature_splits.feature_names, feature_splits.fit.values[index], strict=True))


def test_transaction_available_after_decision_does_not_change_prior_features(feature_store) -> None:
    pair = feature_store
    before = build_feature_splits(pair.operational, _splits())
    before_target = _features_by_id(before, TARGET_TXN)

    insert_operational_record(
        pair.operational,
        "transactions",
        {
            "txn_id": "txn-late-arrival",
            "episode_id": "base",
            "customer_id": "customer-1",
            "merchant_id": "merchant-1",
            "created_at": "2026-01-08T11:45:00Z",
            "event_at": "2026-01-08T11:45:00Z",
            "available_at": "2026-01-08T12:00:01Z",
            "amount": 1000,
            "channel": "qr",
        },
    )
    pair.operational.commit()

    after = build_feature_splits(pair.operational, _splits())
    after_target = _features_by_id(after, TARGET_TXN)
    assert after_target == before_target
    assert before_target["txn_count_1h"] == 1
    assert before_target["txn_count_24h"] == 2
    assert before_target["txn_count_7d"] == 2
    assert before_target["amount_sum_7d"] == 400
    assert before_target["amount_avg_7d"] == 200
    assert before_target["amount_to_customer_historical_avg"] == pytest.approx(3.0)


def test_incomplete_customer_windows_are_flagged_and_full_windows_are_marked(feature_store) -> None:
    features = build_feature_splits(feature_store.operational, _splits())
    early = _features_by_id(features, EARLY_TXN)
    later = _features_by_id(features, TARGET_TXN)

    assert early["history_window_observed_1h"] == 0
    assert early["history_window_observed_24h"] == 0
    assert early["history_window_observed_7d"] == 0
    assert later["history_window_observed_1h"] == 1
    assert later["history_window_observed_24h"] == 1
    assert later["history_window_observed_7d"] == 1


def test_feature_sources_and_columns_exclude_truth_generator_and_private_data(
    feature_store, monkeypatch
) -> None:
    calls: list[str] = []
    input_columns: set[str] = set()
    read_as_of = feature_module.read_as_of

    def tracked_read_as_of(connection, table, **kwargs):
        rows = read_as_of(connection, table, **kwargs)
        enriched_rows = [
            {
                **row,
                "truth_label": "fraud",
                "generator_seed": 123,
                "private_effect": "hidden",
            }
            for row in rows
        ]
        calls.append(table)
        input_columns.update(key for row in enriched_rows for key in row)
        return enriched_rows

    monkeypatch.setattr(feature_module, "read_as_of", tracked_read_as_of)
    features = build_feature_splits(feature_store.operational, _splits())

    assert set(calls) == {"transactions", "customers"}
    assert {"truth_label", "generator_seed", "private_effect"} <= input_columns
    assert features.feature_names == FEATURE_NAMES
    assert not any(
        any(forbidden in name.lower() for forbidden in ("truth", "generator", "private", "metadata"))
        for name in features.feature_names
    )


def test_feature_matrices_are_deterministic(feature_store) -> None:
    first = build_feature_splits(feature_store.operational, _splits())
    second = build_feature_splits(feature_store.operational, _splits())

    assert first == second
    assert first.fit.txn_ids == (EARLY_TXN, TARGET_TXN)
    assert first.fit.targets == (0, 1)
