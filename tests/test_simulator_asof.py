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

    assert txn_ids == {
        "txn-base-before-t0",
        "txn-base-at-boundary",
        "txn-a-at-availability-cutoff",
    }
    assert "txn-base-at-t0" not in txn_ids  # the base branch uses event_at < T0
    assert "txn-a-not-yet-available" not in txn_ids
    assert "txn-b-other-episode" not in txn_ids


def test_lifecycle_events_do_not_reveal_authorization_or_settlement_early(p1_2_store) -> None:
    before_authorization = read_as_of(
        p1_2_store.operational,
        "transaction_events",
        episode_id="ep-a",
        t0=T0,
        as_of_time="2026-01-30T10:00:00.500000Z",
    )
    assert before_authorization == []

    before_settlement = read_as_of(
        p1_2_store.operational,
        "transaction_events",
        episode_id="ep-a",
        t0=T0,
        as_of_time="2026-01-30T10:05:00Z",
    )
    assert [row["event_type"] for row in before_settlement] == ["authorized"]
    assert all(row["event_type"] != "settled" for row in before_settlement)

    transaction = read_as_of(
        p1_2_store.operational,
        "transactions",
        episode_id="ep-a",
        t0=T0,
        as_of_time="2026-01-30T10:00:00.500000Z",
    )[0]
    assert "status" not in transaction
    assert "authorized_at" not in transaction
    assert "settled_at" not in transaction


def test_base_transaction_lifecycle_crosses_t0_in_each_episode(p1_2_store) -> None:
    before_t0 = read_as_of(
        p1_2_store.operational,
        "transaction_events",
        episode_id="ep-a",
        t0=T0,
        as_of_time="2026-01-31T16:59:59.999999Z",
    )
    assert "txn-boundary-authorized-ep-a" not in {row["event_id"] for row in before_t0}

    at_t0 = read_as_of(
        p1_2_store.operational,
        "transaction_events",
        episode_id="ep-a",
        t0=T0,
        as_of_time=T0,
    )
    boundary_event = next(
        row for row in at_t0 if row["event_id"] == "txn-boundary-authorized-ep-a"
    )
    assert boundary_event["txn_id"] == "txn-base-at-boundary"
    assert boundary_event["episode_id"] == "ep-a"
    assert boundary_event["event_at"] == "2026-01-31T17:00:00.000000Z"
    assert boundary_event["available_at"] == "2026-01-31T17:00:00.000000Z"
    assert "txn-boundary-declined-ep-b" not in {
        row["event_id"] for row in at_t0
    }

    after_t0 = read_as_of(
        p1_2_store.operational,
        "transaction_events",
        episode_id="ep-a",
        t0=T0,
        as_of_time="2026-01-31T17:00:00.000001Z",
    )
    assert "txn-boundary-authorized-ep-a" in {row["event_id"] for row in after_t0}

    other_episode = read_as_of(
        p1_2_store.operational,
        "transaction_events",
        episode_id="ep-b",
        t0=T0,
        as_of_time=T0,
    )
    assert "txn-boundary-authorized-ep-a" not in {
        row["event_id"] for row in other_episode
    }
    assert "txn-boundary-declined-ep-b" in {
        row["event_id"] for row in other_episode
    }


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

    lifecycle_events = read_as_of(
        p1_2_store.operational,
        "transaction_events",
        episode_id="ep-a",
        t0=T0,
        as_of_time=AS_OF,
    )
    assert "txn-a-authorized" in {row["event_id"] for row in lifecycle_events}


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
            "event_at": "2026-02-01T00:30:00Z",
            "available_at": "2026-02-02T00:00:00.000001Z",
            "amount": 120000,
            "channel": "qr",
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


def test_null_amount_round_trips_as_null(p1_2_store) -> None:
    insert_operational_record(
        p1_2_store.operational,
        "transactions",
        {
            "txn_id": "txn-null-amount",
            "episode_id": "ep-a",
            "customer_id": "customer-base",
            "merchant_id": "merchant-base",
            "created_at": "2026-02-01T00:03:00Z",
            "event_at": "2026-02-01T00:03:00Z",
            "available_at": "2026-02-01T00:03:00Z",
            "amount": None,
            "channel": "qr",
        },
    )
    rows = read_as_of(
        p1_2_store.operational,
        "transactions",
        episode_id="ep-a",
        t0=T0,
        as_of_time=AS_OF,
    )
    transaction = next(row for row in rows if row["txn_id"] == "txn-null-amount")
    assert transaction["amount"] is None


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

