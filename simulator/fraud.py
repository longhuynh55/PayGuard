"""Generate the P1.4 card-testing mechanism and benign comparison traffic."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from simulator.config import SimulatorConfig
from simulator.randomness import keyed_rng
from simulator.storage import insert_operational_record, insert_private_record


CARD_TESTING_METADATA_KEY = "p1.4.card_testing.v1"


@dataclass(frozen=True)
class FraudSummary:
    pattern_count: int
    small_attempt_count: int
    approved_small_attempt_count: int
    declined_small_attempt_count: int
    larger_followup_count: int
    approved_larger_followup_count: int
    benign_control_transaction_count: int
    transaction_count: int
    lifecycle_event_count: int


def _next_transaction_id(
    business_date: date, customer_id: str, occupied: set[str]
) -> str:
    """Use the same ordinary base transaction ID shape as the background stream."""
    stem = f"txn-base-{business_date:%Y%m%d}-{customer_id}-d"
    sequence = 1
    while f"{stem}{sequence:04d}" in occupied:
        sequence += 1
    txn_id = f"{stem}{sequence:04d}"
    occupied.add(txn_id)
    return txn_id


def _insert_transaction(
    operational: sqlite3.Connection,
    *,
    txn_id: str,
    event_ids: set[str],
    customer_id: str,
    merchant_id: str,
    channel: str,
    amount: int,
    created_at: datetime,
    authorization_delay: timedelta,
    settlement_delay: timedelta,
    approved: bool,
    authorization_response_code: str,
) -> int:
    """Write a transaction and its observable lifecycle through P1.2 storage."""
    insert_operational_record(
        operational,
        "transactions",
        {
            "txn_id": txn_id,
            "episode_id": "base",
            "customer_id": customer_id,
            "merchant_id": merchant_id,
            "created_at": created_at,
            "event_at": created_at,
            "available_at": created_at,
            "amount": amount,
            "channel": channel,
        },
    )

    authorized_at = created_at + authorization_delay
    authorization_type = "authorized" if approved else "declined"
    authorization_event_id = f"{txn_id}:{authorization_type}"
    if authorization_event_id in event_ids:
        raise ValueError(f"duplicate lifecycle event ID: {authorization_event_id}")
    event_ids.add(authorization_event_id)
    insert_operational_record(
        operational,
        "transaction_events",
        {
            "event_id": authorization_event_id,
            "txn_id": txn_id,
            "episode_id": "base",
            "event_type": authorization_type,
            "event_at": authorized_at,
            "available_at": authorized_at,
            "response_code": authorization_response_code if approved else None,
        },
    )
    if not approved:
        return 1

    settlement_event_id = f"{txn_id}:settled"
    if settlement_event_id in event_ids:
        raise ValueError(f"duplicate lifecycle event ID: {settlement_event_id}")
    event_ids.add(settlement_event_id)
    settled_at = authorized_at + settlement_delay
    insert_operational_record(
        operational,
        "transaction_events",
        {
            "event_id": settlement_event_id,
            "txn_id": txn_id,
            "episode_id": "base",
            "event_type": "settled",
            "event_at": settled_at,
            "available_at": settled_at,
            "response_code": None,
        },
    )
    return 2


def generate_card_testing(
    operational: sqlite3.Connection,
    private: sqlite3.Connection,
    config: SimulatorConfig,
    *,
    seed: int | None = None,
) -> FraudSummary:
    """Append probabilistic card testing and similar benign traffic to base history.

    Card attempts and their later larger transactions use channel ``card``. Each
    small attempt and larger follow-up has an independent keyed approval draw.
    A benign control emits the same number and timing of small card transactions,
    all successfully authorized and settled, with no larger follow-up.

    Mechanism, role, seed, and entity-to-pattern membership are stored only in
    the private ``run_metadata`` export. Operational IDs use the ordinary base
    transaction format and do not encode a fraud mechanism or label.
    """
    if operational is private:
        raise ValueError("fraud generation requires separate operational and private connections")

    mechanism = config.fraud.card_testing
    run_seed = config.seed.value if seed is None else seed
    if isinstance(run_seed, bool) or not isinstance(run_seed, int) or run_seed < 0:
        raise ValueError("fraud seed must be a non-negative integer")
    if private.execute(
        "SELECT 1 FROM run_metadata WHERE key = ?", (CARD_TESTING_METADATA_KEY,)
    ).fetchone():
        raise ValueError("card-testing generation has already been recorded in this private export")

    customer_ids = [
        row[0]
        for row in operational.execute(
            "SELECT customer_id FROM customers WHERE episode_id = 'base' ORDER BY customer_id"
        )
    ]
    merchant_ids = [
        row[0]
        for row in operational.execute(
            "SELECT merchant_id FROM merchants WHERE episode_id = 'base' ORDER BY merchant_id"
        )
    ]
    pattern_count = mechanism.patterns_per_run.value
    if len(customer_ids) < pattern_count * 2:
        raise ValueError("card-testing generation requires distinct base customers for fraud and controls")
    if not merchant_ids:
        raise ValueError("generate_world must create base merchants before fraud generation")

    existing_txn_ids = {
        row[0] for row in operational.execute("SELECT txn_id FROM transactions")
    }
    existing_event_ids = {
        row[0] for row in operational.execute("SELECT event_id FROM transaction_events")
    }
    txn_ids = set(existing_txn_ids)
    event_ids = set(existing_event_ids)

    selection_rng = keyed_rng(run_seed, "p1.fraud.card_testing.entity_selection")
    selected_customers = selection_rng.sample(customer_ids, pattern_count * 2)
    selected_merchants = [selection_rng.choice(merchant_ids) for _ in range(pattern_count * 2)]

    business_timezone = ZoneInfo(config.timeline.business_timezone.value)
    t0 = config.timeline.t0_business_date.value
    pattern_date = t0 - timedelta(days=1)
    window = timedelta(minutes=mechanism.window_minutes.value)
    followup_delay = timedelta(seconds=mechanism.followup_delay_seconds.value)
    authorization_delay = timedelta(seconds=config.transactions.authorization_delay_seconds.value)
    settlement_delay = timedelta(minutes=config.transactions.settlement_delay_minutes.value)
    spacing_seconds = (mechanism.window_minutes.value * 60) // mechanism.attempts_per_card.value
    authorization_response_code = config.transactions.authorization_response_code.value

    group_metadata: list[dict[str, object]] = []
    small_attempt_count = 0
    approved_small_attempt_count = 0
    declined_small_attempt_count = 0
    larger_followup_count = 0
    approved_larger_followup_count = 0
    benign_control_transaction_count = 0
    transaction_count = 0
    lifecycle_event_count = 0

    operational.execute("SAVEPOINT p1_4_card_testing")
    private.execute("SAVEPOINT p1_4_card_testing_metadata")
    try:
        for pattern_index in range(pattern_count):
            for role in ("fraud", "benign_control"):
                entity_slot = pattern_index * 2 + int(role == "benign_control")
                customer_id = selected_customers[entity_slot]
                merchant_id = selected_merchants[entity_slot]
                component = (
                    "p1.fraud.card_testing"
                    if role == "fraud"
                    else "p1.fraud.benign_control"
                )
                group_start = datetime.combine(
                    pattern_date,
                    time(mechanism.start_hour_local.value, 0),
                    tzinfo=business_timezone,
                )
                if role == "benign_control":
                    group_start += timedelta(
                        minutes=mechanism.benign_control_delay_minutes.value
                    )
                small_ids: list[str] = []
                group_approved_small = 0
                group_declined_small = 0
                group_event_count = 0

                for attempt_index in range(mechanism.attempts_per_card.value):
                    created_at = group_start + timedelta(
                        seconds=attempt_index * spacing_seconds
                    )
                    amount_rng = keyed_rng(
                        run_seed, f"{component}.amount", pattern_index, "small", attempt_index
                    )
                    amount = amount_rng.randint(
                        mechanism.minimum_attempt_amount.value,
                        mechanism.maximum_attempt_amount.value,
                    )
                    txn_id = _next_transaction_id(pattern_date, customer_id, txn_ids)
                    if role == "fraud":
                        outcome_rng = keyed_rng(
                            run_seed,
                            "p1.fraud.card_testing.approval",
                            pattern_index,
                            attempt_index,
                        )
                        approved = outcome_rng.random() < mechanism.approval_probability.value
                    else:
                        approved = True

                    group_event_count += _insert_transaction(
                        operational,
                        txn_id=txn_id,
                        event_ids=event_ids,
                        customer_id=customer_id,
                        merchant_id=merchant_id,
                        channel=mechanism.channel.value,
                        amount=amount,
                        created_at=created_at,
                        authorization_delay=authorization_delay,
                        settlement_delay=settlement_delay,
                        approved=approved,
                        authorization_response_code=authorization_response_code,
                    )
                    small_ids.append(txn_id)
                    transaction_count += 1
                    if role == "fraud":
                        small_attempt_count += 1
                        if approved:
                            approved_small_attempt_count += 1
                            group_approved_small += 1
                        else:
                            declined_small_attempt_count += 1
                            group_declined_small += 1
                    else:
                        benign_control_transaction_count += 1

                metadata: dict[str, object] = {
                    "role": role,
                    "pattern_id": f"card-testing-{pattern_index:04d}-{role}",
                    "customer_id": customer_id,
                    "merchant_id": merchant_id,
                    "small_transaction_ids": small_ids,
                    "small_transaction_count": len(small_ids),
                    "approved_small_attempt_count": group_approved_small,
                    "declined_small_attempt_count": group_declined_small,
                    "lifecycle_event_count": group_event_count,
                    "start_at": group_start.isoformat(),
                }

                if role == "fraud":
                    followup_created_at = group_start + window + followup_delay
                    followup_amount_rng = keyed_rng(
                        run_seed, "p1.fraud.card_testing.amount", pattern_index, "followup"
                    )
                    followup_amount = followup_amount_rng.randint(
                        mechanism.minimum_followup_amount.value,
                        mechanism.maximum_followup_amount.value,
                    )
                    followup_txn_id = _next_transaction_id(
                        pattern_date, customer_id, txn_ids
                    )
                    followup_outcome_rng = keyed_rng(
                        run_seed, "p1.fraud.card_testing.approval", pattern_index, "followup"
                    )
                    followup_approved = (
                        followup_outcome_rng.random()
                        < mechanism.followup_approval_probability.value
                    )
                    followup_events = _insert_transaction(
                        operational,
                        txn_id=followup_txn_id,
                        event_ids=event_ids,
                        customer_id=customer_id,
                        merchant_id=merchant_id,
                        channel=mechanism.channel.value,
                        amount=followup_amount,
                        created_at=followup_created_at,
                        authorization_delay=authorization_delay,
                        settlement_delay=settlement_delay,
                        approved=followup_approved,
                        authorization_response_code=authorization_response_code,
                    )
                    transaction_count += 1
                    larger_followup_count += 1
                    if followup_approved:
                        approved_larger_followup_count += 1
                    group_event_count += followup_events
                    metadata.update(
                        {
                            "mechanism": "card_testing",
                            "larger_followup_transaction_id": followup_txn_id,
                            "larger_followup_amount": followup_amount,
                            "larger_followup_approved": followup_approved,
                            "larger_followup_count": 1,
                            "end_at": followup_created_at.isoformat(),
                        }
                    )
                else:
                    metadata["end_at"] = (
                        group_start + timedelta(seconds=(mechanism.attempts_per_card.value - 1) * spacing_seconds)
                    ).isoformat()

                metadata["lifecycle_event_count"] = group_event_count
                group_metadata.append(metadata)
                lifecycle_event_count += group_event_count

        summary = FraudSummary(
            pattern_count=pattern_count,
            small_attempt_count=small_attempt_count,
            approved_small_attempt_count=approved_small_attempt_count,
            declined_small_attempt_count=declined_small_attempt_count,
            larger_followup_count=larger_followup_count,
            approved_larger_followup_count=approved_larger_followup_count,
            benign_control_transaction_count=benign_control_transaction_count,
            transaction_count=transaction_count,
            lifecycle_event_count=lifecycle_event_count,
        )
        insert_private_record(
            private,
            "run_metadata",
            {
                "key": CARD_TESTING_METADATA_KEY,
                "value_json": {
                    "algorithm": "p1.4-card-testing-v1",
                    "seed": run_seed,
                    "mechanism": "card_testing",
                    "channel": mechanism.channel.value,
                    "summary": asdict(summary),
                    "groups": group_metadata,
                },
            },
        )
        private.execute("RELEASE SAVEPOINT p1_4_card_testing_metadata")
        operational.execute("RELEASE SAVEPOINT p1_4_card_testing")
    except Exception:
        private.execute("ROLLBACK TO SAVEPOINT p1_4_card_testing_metadata")
        private.execute("RELEASE SAVEPOINT p1_4_card_testing_metadata")
        operational.execute("ROLLBACK TO SAVEPOINT p1_4_card_testing")
        operational.execute("RELEASE SAVEPOINT p1_4_card_testing")
        raise

    return summary
