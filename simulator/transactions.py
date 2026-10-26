"""Generate the valid shared-history transaction stream for P1.3 round 1."""

from __future__ import annotations

import math
import random
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from simulator.config import ProfileName, SimulatorConfig
from simulator.randomness import keyed_rng
from simulator.storage import insert_operational_record


@dataclass(frozen=True)
class TransactionSummary:
    transaction_count: int
    lifecycle_event_count: int


def _poisson_count(mean: float, rng: random.Random) -> int:
    """Draw a Poisson count with Knuth's exact product method."""
    threshold = math.exp(-mean)
    product = 1.0
    count = 0
    while product > threshold:
        count += 1
        product *= rng.random()
    return count - 1


def _local_dates(start: date, end_exclusive: date):
    current = start
    while current < end_exclusive:
        yield current
        current += timedelta(days=1)


def generate_transactions(
    connection: sqlite3.Connection,
    config: SimulatorConfig,
    profile: ProfileName = "smoke",
    *,
    seed: int | None = None,
) -> TransactionSummary:
    """Insert valid base transactions and their authorization/settlement events.

    Counts are sampled independently per customer and Vietnam business date.
    A transaction's hour, amount, and merchant use separate keyed RNG streams,
    so changing one behavior parameter does not consume another component's RNG.
    """
    run_seed = config.seed.value if seed is None else seed
    # Resolve the profile here as well as in world generation so a bad profile
    # fails before any transaction rows are written.
    config.scale_for(profile)
    start_date = config.timeline.start_business_date.value
    t0_date = config.timeline.t0_business_date.value
    if start_date >= t0_date:
        raise ValueError("transaction generation requires at least one configured business day before T0")

    if connection.execute("SELECT 1 FROM transactions LIMIT 1").fetchone():
        raise ValueError("transaction generation requires an empty transactions table")
    if connection.execute("SELECT 1 FROM transaction_events LIMIT 1").fetchone():
        raise ValueError("transaction generation requires an empty transaction_events table")

    customer_ids = tuple(
        row[0]
        for row in connection.execute(
            "SELECT customer_id FROM customers WHERE episode_id = 'base' ORDER BY customer_id"
        )
    )
    merchant_ids = tuple(
        row[0]
        for row in connection.execute(
            "SELECT merchant_id FROM merchants WHERE episode_id = 'base' ORDER BY merchant_id"
        )
    )
    if not customer_ids or not merchant_ids:
        raise ValueError("generate_world must create base customers and merchants first")
    expected_scale = config.scale_for(profile)
    if len(customer_ids) != expected_scale.customer_count.value:
        raise ValueError("base customer count does not match the selected profile")
    if len(merchant_ids) != expected_scale.merchant_count.value:
        raise ValueError("base merchant count does not match the selected profile")

    business_tz = ZoneInfo(config.timeline.business_timezone.value)
    t0_local = datetime.combine(t0_date, time.min).replace(tzinfo=business_tz)
    latest_creation_utc = (
        t0_local.astimezone(UTC)
        - timedelta(
            minutes=config.transactions.settlement_delay_minutes.value,
            seconds=config.transactions.authorization_delay_seconds.value,
            microseconds=1,
        )
    )
    daily_mean = config.world.expected_transactions_per_customer_local_day.value
    hourly_weights = config.transactions.hourly_transaction_weights.value
    min_amount = config.transactions.minimum_amount.value
    max_amount = config.transactions.maximum_amount.value
    channel = config.transactions.channel.value
    authorization_response_code = config.transactions.authorization_response_code.value
    authorization_delay = timedelta(seconds=config.transactions.authorization_delay_seconds.value)
    settlement_delay = timedelta(minutes=config.transactions.settlement_delay_minutes.value)

    transaction_count = 0
    for business_date in _local_dates(start_date, t0_date):
        for customer_id in customer_ids:
            count_rng = keyed_rng(
                run_seed,
                "p1.transactions.daily_count",
                business_date.isoformat(),
                customer_id,
            )
            count = _poisson_count(daily_mean, count_rng)
            for daily_index in range(1, count + 1):
                txn_id = (
                    f"txn-base-{business_date:%Y%m%d}-{customer_id}-d{daily_index:04d}"
                )
                time_rng = keyed_rng(run_seed, "p1.transactions.time", txn_id)
                hour = time_rng.choices(range(24), weights=hourly_weights, k=1)[0]
                local_created_at = datetime.combine(
                    business_date,
                    time(hour, time_rng.randrange(60), time_rng.randrange(60)),
                ).replace(tzinfo=business_tz)
                created_at_utc = local_created_at.astimezone(UTC)
                if created_at_utc > latest_creation_utc:
                    # Keep every base lifecycle event strictly before T0. Only
                    # the final configured day can be affected by this margin.
                    continue

                merchant_rng = keyed_rng(run_seed, "p1.transactions.merchant", txn_id)
                amount_rng = keyed_rng(run_seed, "p1.transactions.amount", txn_id)
                merchant_id = merchant_rng.choice(merchant_ids)
                amount = amount_rng.randint(min_amount, max_amount)

                insert_operational_record(
                    connection,
                    "transactions",
                    {
                        "txn_id": txn_id,
                        "episode_id": "base",
                        "customer_id": customer_id,
                        "merchant_id": merchant_id,
                        "created_at": created_at_utc,
                        "event_at": created_at_utc,
                        "available_at": created_at_utc,
                        "amount": amount,
                        "channel": channel,
                    },
                )

                authorized_at = created_at_utc + authorization_delay
                settled_at = authorized_at + settlement_delay
                insert_operational_record(
                    connection,
                    "transaction_events",
                    {
                        "event_id": f"{txn_id}:authorized",
                        "txn_id": txn_id,
                        "episode_id": "base",
                        "event_type": "authorized",
                        "event_at": authorized_at,
                        "available_at": authorized_at,
                        "response_code": authorization_response_code,
                    },
                )
                insert_operational_record(
                    connection,
                    "transaction_events",
                    {
                        "event_id": f"{txn_id}:settled",
                        "txn_id": txn_id,
                        "episode_id": "base",
                        "event_type": "settled",
                        "event_at": settled_at,
                        "available_at": settled_at,
                        "response_code": None,
                    },
                )
                transaction_count += 1

    return TransactionSummary(transaction_count, transaction_count * 2)


def generate_episode_transactions(
    connection: sqlite3.Connection,
    config: SimulatorConfig,
    profile: ProfileName,
    *,
    episode_id: str,
    seed: int,
    duration_days: int,
) -> TransactionSummary:
    """Generate ordinary transactions for one post-T0 parallel episode."""
    if not episode_id or episode_id == "base":
        raise ValueError("episode transaction generation requires a non-base episode_id")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("episode transaction seed must be a non-negative integer")
    if isinstance(duration_days, bool) or not isinstance(duration_days, int) or duration_days <= 0:
        raise ValueError("duration_days must be a positive integer")
    if connection.execute(
        "SELECT 1 FROM episodes WHERE episode_id = ? AND role <> 'base'", (episode_id,)
    ).fetchone() is None:
        raise ValueError("episode transaction generation requires a registered episode")
    if connection.execute(
        "SELECT 1 FROM transactions WHERE episode_id = ? LIMIT 1", (episode_id,)
    ).fetchone():
        raise ValueError("episode transaction generation requires an empty episode transaction stream")

    expected_scale = config.scale_for(profile)
    customer_ids = tuple(
        row[0]
        for row in connection.execute(
            "SELECT customer_id FROM customers WHERE episode_id = 'base' ORDER BY customer_id"
        )
    )
    merchant_ids = tuple(
        row[0]
        for row in connection.execute(
            "SELECT merchant_id FROM merchants WHERE episode_id = 'base' ORDER BY merchant_id"
        )
    )
    if len(customer_ids) != expected_scale.customer_count.value:
        raise ValueError("base customer count does not match the selected profile")
    if len(merchant_ids) != expected_scale.merchant_count.value:
        raise ValueError("base merchant count does not match the selected profile")

    business_tz = ZoneInfo(config.timeline.business_timezone.value)
    t0_date = config.timeline.t0_business_date.value
    end_date = t0_date + timedelta(days=duration_days)
    end_utc = datetime.combine(end_date, time.min).replace(tzinfo=business_tz).astimezone(UTC)
    lifecycle_tail = timedelta(
        seconds=config.transactions.authorization_delay_seconds.value,
        minutes=config.transactions.settlement_delay_minutes.value,
    )
    latest_creation_utc = end_utc - lifecycle_tail - timedelta(microseconds=1)
    daily_mean = config.world.expected_transactions_per_customer_local_day.value
    hourly_weights = config.transactions.hourly_transaction_weights.value
    authorization_delay = timedelta(seconds=config.transactions.authorization_delay_seconds.value)
    settlement_delay = timedelta(minutes=config.transactions.settlement_delay_minutes.value)
    transaction_count = 0

    for business_date in _local_dates(t0_date, end_date):
        for customer_id in customer_ids:
            count_rng = keyed_rng(
                seed,
                "p1.7.transactions.daily_count",
                episode_id,
                business_date.isoformat(),
                customer_id,
            )
            count = _poisson_count(daily_mean, count_rng)
            for daily_index in range(1, count + 1):
                txn_id = (
                    f"txn-{episode_id}-{business_date:%Y%m%d}-{customer_id}-d{daily_index:04d}"
                )
                time_rng = keyed_rng(seed, "p1.7.transactions.time", episode_id, txn_id)
                hour = time_rng.choices(range(24), weights=hourly_weights, k=1)[0]
                local_created_at = datetime.combine(
                    business_date,
                    time(hour, time_rng.randrange(60), time_rng.randrange(60)),
                ).replace(tzinfo=business_tz)
                created_at = local_created_at.astimezone(UTC)
                if created_at > latest_creation_utc:
                    continue

                merchant_id = keyed_rng(
                    seed, "p1.7.transactions.merchant", episode_id, txn_id
                ).choice(merchant_ids)
                amount = keyed_rng(
                    seed, "p1.7.transactions.amount", episode_id, txn_id
                ).randint(
                    config.transactions.minimum_amount.value,
                    config.transactions.maximum_amount.value,
                )
                insert_operational_record(
                    connection,
                    "transactions",
                    {
                        "txn_id": txn_id,
                        "episode_id": episode_id,
                        "customer_id": customer_id,
                        "merchant_id": merchant_id,
                        "created_at": created_at,
                        "event_at": created_at,
                        "available_at": created_at,
                        "amount": amount,
                        "channel": config.transactions.channel.value,
                    },
                )
                authorized_at = created_at + authorization_delay
                settled_at = authorized_at + settlement_delay
                insert_operational_record(
                    connection,
                    "transaction_events",
                    {
                        "event_id": f"{txn_id}:authorized",
                        "txn_id": txn_id,
                        "episode_id": episode_id,
                        "event_type": "authorized",
                        "event_at": authorized_at,
                        "available_at": authorized_at,
                        "response_code": config.transactions.authorization_response_code.value,
                    },
                )
                insert_operational_record(
                    connection,
                    "transaction_events",
                    {
                        "event_id": f"{txn_id}:settled",
                        "txn_id": txn_id,
                        "episode_id": episode_id,
                        "event_type": "settled",
                        "event_at": settled_at,
                        "available_at": settled_at,
                        "response_code": None,
                    },
                )
                transaction_count += 1

    return TransactionSummary(transaction_count, transaction_count * 2)
