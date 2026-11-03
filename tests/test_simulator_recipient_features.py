"""Functional contract of ``derive_recipient_signals`` for new or dormant recipients."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from simulator.recipient_features import derive_recipient_signals
from simulator.storage import create_export_stores, insert_operational_record


T0 = "2026-01-01T00:00:00Z"
AS_OF = "2026-01-25T12:00:00Z"
EPISODE_ID = "ep-recipient"
LONG_AGO = "2025-06-01T00:00:00Z"

BASE_CUSTOMERS = (
    "dormant-recipient",
    "old-sender",
    "burst-sender-1",
    "burst-sender-2",
    "burst-sender-3",
    "active-recipient",
    "counterparty-out",
    "counterparty-in",
    "quiet-dormant-recipient",
    "quiet-sender",
    "quiet-active-recipient",
    "quiet-counterparty",
)


def _customer(customer_id: str, episode_id: str, event_at: str) -> dict[str, Any]:
    return {
        "customer_id": customer_id,
        "episode_id": episode_id,
        "event_at": event_at,
        "available_at": event_at,
        "segment": "retail",
        "province": "HCM",
        "age_band": None,
    }


def _transfer(
    txn_id: str, episode_id: str, sender: str, recipient: str, created_at: str, amount: int
) -> dict[str, Any]:
    return {
        "txn_id": txn_id,
        "episode_id": episode_id,
        "customer_id": sender,
        "merchant_id": None,
        "recipient_customer_id": recipient,
        "created_at": created_at,
        "event_at": created_at,
        "available_at": created_at,
        "amount": amount,
        "channel": "wallet",
    }


@pytest.fixture
def recipient_store(tmp_path: Path):
    pair = create_export_stores(tmp_path / "recipient-run")
    try:
        operational = pair.operational
        insert_operational_record(
            operational, "episodes", {"episode_id": EPISODE_ID, "role": "development"}
        )
        for customer_id in BASE_CUSTOMERS:
            insert_operational_record(operational, "customers", _customer(customer_id, "base", LONG_AGO))
        base_transfers = (
            # Dormant account: its last activity is about two months before the burst.
            _transfer("base-to-dormant", "base", "old-sender", "dormant-recipient", "2025-11-15T08:00:00Z", 100000),
            # Account with no incoming transfer in the 24 hours before AS_OF.
            _transfer("base-to-quiet-dormant", "base", "quiet-sender", "quiet-dormant-recipient", "2025-11-10T08:00:00Z", 80000),
        )
        episode_transfers = (
            # Burst into the long-dormant account: three receipts in the 24 hours before AS_OF.
            _transfer("burst-1", EPISODE_ID, "burst-sender-1", "dormant-recipient", "2026-01-25T02:00:00Z", 1000000),
            _transfer("burst-2", EPISODE_ID, "burst-sender-2", "dormant-recipient", "2026-01-25T07:00:00Z", 2000000),
            _transfer("burst-3", EPISODE_ID, "burst-sender-3", "dormant-recipient", "2026-01-25T11:30:00Z", 3000000),
            # Active account: outgoing 15 days before its receipt, then a receipt in the window.
            _transfer("active-out", EPISODE_ID, "active-recipient", "counterparty-out", "2026-01-10T09:00:00Z", 50000),
            _transfer("active-in", EPISODE_ID, "counterparty-in", "active-recipient", "2026-01-25T03:00:00Z", 200000),
            # Recently active account with no receipt in the 24 hours before AS_OF.
            _transfer("quiet-recent-in", EPISODE_ID, "quiet-counterparty", "quiet-active-recipient", "2026-01-21T10:00:00Z", 30000),
            _transfer("quiet-recent-out", EPISODE_ID, "quiet-active-recipient", "quiet-counterparty", "2026-01-22T10:00:00Z", 20000),
        )
        for row in base_transfers:
            insert_operational_record(operational, "transactions", row)
        for row in episode_transfers:
            insert_operational_record(operational, "transactions", row)
        operational.commit()
        yield pair
    finally:
        pair.close()


def test_dormant_account_receiving_a_burst_is_flagged_at_its_first_receipt(recipient_store) -> None:
    signals = derive_recipient_signals(
        recipient_store.operational, episode_id=EPISODE_ID, t0=T0, as_of_time=AS_OF
    )

    burst = signals["dormant-recipient"]
    assert burst.fan_in_7d == 3
    assert burst.new_or_dormant is True


def test_active_account_receiving_is_not_flagged(recipient_store) -> None:
    signals = derive_recipient_signals(
        recipient_store.operational, episode_id=EPISODE_ID, t0=T0, as_of_time=AS_OF
    )

    assert signals["active-recipient"].fan_in_7d == 1
    assert signals["active-recipient"].new_or_dormant is False


def test_account_without_recent_incoming_keeps_the_as_of_value(recipient_store) -> None:
    signals = derive_recipient_signals(
        recipient_store.operational, episode_id=EPISODE_ID, t0=T0, as_of_time=AS_OF
    )

    # No incoming transfer in the 24 hours before AS_OF: the flag is measured at AS_OF.
    assert signals["quiet-dormant-recipient"].fan_in_7d == 0
    assert signals["quiet-dormant-recipient"].new_or_dormant is True
    assert signals["quiet-active-recipient"].fan_in_7d == 1
    assert signals["quiet-active-recipient"].new_or_dormant is False
