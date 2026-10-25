from __future__ import annotations

import pytest

from simulator.asof import read_as_of
from simulator.storage import StorageError, insert_operational_record


T0 = "2026-01-31T17:00:00Z"
AS_OF = "2026-02-02T00:00:00Z"


def test_episode_asof_uses_base_before_t0_and_only_available_rows(p1_2_store) -> None:
    rows = read_as_of(
        p1_2_store.operational,
        "transactions",
        episode_id="ep-a",
        t0=T0,
        as_of_time=AS_OF,
    )
    txn_ids = {row["txn_id"] for row in rows}

    assert txn_ids == {"txn-base-before-t0", "txn-a-at-availability-cutoff"}
    assert "txn-base-at-t0" not in txn_ids  # the base branch uses event_at < T0
    assert "txn-a-not-yet-available" not in txn_ids
    assert "txn-b-other-episode" not in txn_ids


def test_availability_equal_to_cutoff_is_included(p1_2_store) -> None:
    transactions = read_as_of(
        p1_2_store.operational,
        "transactions",
        episode_id="ep-a",
        t0=T0,
        as_of_time=AS_OF,
    )
    assert "txn-a-at-availability-cutoff" in {row["txn_id"] for row in transactions}

    labels = read_as_of(
        p1_2_store.operational,
        "label_events",
        episode_id="ep-a",
        t0=T0,
        as_of_time=AS_OF,
    )
    assert [row["event_id"] for row in labels] == ["label-a-at-availability-cutoff"]
    assert labels[0]["available_at"] == "2026-02-02T00:00:00.000000Z"


def test_base_label_after_cutoff_is_hidden_and_pending_is_not_a_negative(p1_2_store) -> None:
    labels = read_as_of(
        p1_2_store.operational,
        "label_events",
        episode_id="ep-a",
        t0=T0,
        as_of_time=AS_OF,
    )
    event_ids = {row["event_id"] for row in labels}
    assert "label-base-delayed" not in event_ids
    assert "label-a-at-availability-cutoff" in event_ids
    assert "pending" not in {row["label"] for row in labels}
    assert "matured_negative" not in {row["label"] for row in labels}


def test_adding_a_later_available_record_does_not_change_the_snapshot(p1_2_store) -> None:
    before = read_as_of(
        p1_2_store.operational,
        "transactions",
        episode_id="ep-a",
        t0=T0,
        as_of_time=AS_OF,
    )
    insert_operational_record(
        p1_2_store.operational,
        "transactions",
        {
            "txn_id": "txn-a-added-later",
            "episode_id": "ep-a",
            "customer_id": "customer-base",
            "merchant_id": "merchant-base",
            "created_at": "2026-02-01T00:30:00Z",
            "authorized_at": None,
            "settled_at": None,
            "event_at": "2026-02-01T00:30:00Z",
            "available_at": "2026-02-02T00:00:00.000001Z",
            "amount": 120000,
            "channel": "qr",
            "status": "created",
            "response_code": None,
        },
    )
    after = read_as_of(
        p1_2_store.operational,
        "transactions",
        episode_id="ep-a",
        t0=T0,
        as_of_time=AS_OF,
    )

    assert after == before


@pytest.mark.parametrize("episode_id", ["base", "unregistered"])
def test_asof_requires_a_registered_non_base_episode(p1_2_store, episode_id: str) -> None:
    with pytest.raises(StorageError, match="episode_id|registered"):
        read_as_of(
            p1_2_store.operational,
            "transactions",
            episode_id=episode_id,
            t0=T0,
            as_of_time=AS_OF,
        )


def test_asof_rejects_non_temporal_or_unknown_table(p1_2_store) -> None:
    with pytest.raises(StorageError, match="not an as-of operational table"):
        read_as_of(
            p1_2_store.operational,
            "episodes",
            episode_id="ep-a",
            t0=T0,
            as_of_time=AS_OF,
        )
    with pytest.raises(StorageError, match="not an as-of operational table"):
        read_as_of(
            p1_2_store.operational,
            'transactions"; SELECT 1; --',
            episode_id="ep-a",
            t0=T0,
            as_of_time=AS_OF,
        )

