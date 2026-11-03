"""Observable recipient signals derived from operational transactions as of a snapshot."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import timedelta

from simulator.asof import read_as_of
from simulator.common import parse_timestamp
from simulator.storage import utc_timestamp


@dataclass(frozen=True)
class RecipientSignals:
    """Account-level features available from a point-in-time transaction snapshot."""

    recipient_customer_id: str
    fan_in_7d: int
    older_sender_share_7d: float | None
    incoming_amount_24h: int
    outgoing_amount_24h: int
    outflow_ratio_24h: float | None
    new_or_dormant: bool
    first_counterparty_transfer_txn_ids: tuple[str, ...]


def derive_recipient_signals(
    operational: sqlite3.Connection,
    *,
    episode_id: str,
    t0: str,
    as_of_time: str,
    dormant_after_days: int = 30,
) -> dict[str, RecipientSignals]:
    """Derive fan-in, cash-out, new/dormant, and first-counterparty signals.

    All inputs pass through ``read_as_of``. Amounts use the transaction rows that
    are available at the requested snapshot and have event times in the relevant
    trailing window; the function does not read labels or private metadata.

    ``new_or_dormant`` is decided at an anchor, the transaction under evaluation:
    the first incoming transfer to the account in the 24 hours up to
    ``as_of_time`` (``as_of - 24h < created_at <= as_of``, earliest by
    ``created_at`` then ``txn_id``). The account is new or dormant when it was
    created within ``dormant_after_days`` before the anchor, or when no transaction
    into or out of the account happened strictly before the anchor within
    ``dormant_after_days`` before it. The anchor is excluded from that history, so
    a burst of receipts after a long dormancy is flagged at its first receipt. An
    account with no incoming transfer in that window has no transaction under
    evaluation; its flag keeps the earlier rule, measured at ``as_of_time`` over all
    snapshot activity.
    """
    if isinstance(dormant_after_days, bool) or not isinstance(dormant_after_days, int) or dormant_after_days <= 0:
        raise ValueError("dormant_after_days must be a positive integer")
    normalized_as_of = utc_timestamp(as_of_time)
    as_of = parse_timestamp(normalized_as_of)
    transactions = read_as_of(
        operational,
        "transactions",
        episode_id=episode_id,
        t0=t0,
        as_of_time=normalized_as_of,
    )
    customers = read_as_of(
        operational,
        "customers",
        episode_id=episode_id,
        t0=t0,
        as_of_time=normalized_as_of,
    )
    customers_by_id = {row["customer_id"]: row for row in customers}
    transfers = [
        row for row in transactions
        if row["merchant_id"] is None and row["recipient_customer_id"] is not None
    ]
    ordered_transfers = sorted(
        transfers, key=lambda row: (row["created_at"], row["txn_id"])
    )
    first_by_pair: dict[tuple[str, str], str] = {}
    for row in ordered_transfers:
        pair = (row["customer_id"], row["recipient_customer_id"])
        first_by_pair.setdefault(pair, row["txn_id"])

    trailing_7d = as_of - timedelta(days=7)
    trailing_24h = as_of - timedelta(hours=24)
    dormant_cutoff = as_of - timedelta(days=dormant_after_days)
    signals: dict[str, RecipientSignals] = {}
    recipient_ids = sorted({row["recipient_customer_id"] for row in transfers})
    for recipient_id in recipient_ids:
        account_transfers = [row for row in transfers if row["recipient_customer_id"] == recipient_id]
        recent_incoming = [
            row for row in account_transfers
            if trailing_24h < parse_timestamp(row["created_at"]) <= as_of
        ]
        recent_outgoing = [
            row for row in transactions
            if row["customer_id"] == recipient_id
            and trailing_24h < parse_timestamp(row["created_at"]) <= as_of
            and row["recipient_customer_id"] is not None
        ]
        incoming_amount = sum(
            row["amount"] for row in recent_incoming if type(row["amount"]) is int
        )
        outgoing_amount = sum(
            row["amount"] for row in recent_outgoing if type(row["amount"]) is int
        )
        recent_fan_in = [
            row for row in account_transfers
            if trailing_7d < parse_timestamp(row["created_at"]) <= as_of
        ]
        fan_in = len({row["customer_id"] for row in recent_fan_in})
        sender_ids = {row["customer_id"] for row in recent_fan_in}
        older_senders = sum(
            customers_by_id.get(sender_id, {}).get("age_band") == "55+"
            for sender_id in sender_ids
        )
        customer = customers_by_id.get(recipient_id)
        account_created_at = parse_timestamp(customer["event_at"]) if customer else None
        activity = [
            parse_timestamp(row["created_at"])
            for row in transactions
            if row["customer_id"] == recipient_id or row["recipient_customer_id"] == recipient_id
        ]
        if recent_incoming:
            anchor = min(
                recent_incoming,
                key=lambda row: (parse_timestamp(row["created_at"]), row["txn_id"]),
            )
            anchor_at = parse_timestamp(anchor["created_at"])
            anchor_cutoff = anchor_at - timedelta(days=dormant_after_days)
            last_prior_activity = max(
                (at for at in activity if at < anchor_at), default=None
            )
            new_or_dormant = (
                account_created_at is None
                or account_created_at > anchor_cutoff
                or last_prior_activity is None
                or last_prior_activity <= anchor_cutoff
            )
        else:
            last_activity = max(activity, default=None)
            new_or_dormant = (
                account_created_at is None
                or account_created_at > dormant_cutoff
                or last_activity is None
                or last_activity <= dormant_cutoff
            )
        signals[recipient_id] = RecipientSignals(
            recipient_customer_id=recipient_id,
            fan_in_7d=fan_in,
            older_sender_share_7d=(older_senders / fan_in if fan_in else None),
            incoming_amount_24h=incoming_amount,
            outgoing_amount_24h=outgoing_amount,
            outflow_ratio_24h=(outgoing_amount / incoming_amount if incoming_amount else None),
            new_or_dormant=new_or_dormant,
            first_counterparty_transfer_txn_ids=tuple(
                row["txn_id"]
                for row in account_transfers
                if first_by_pair[(row["customer_id"], recipient_id)] == row["txn_id"]
            ),
        )
    return signals
