"""Generate the valid shared-history transaction stream for P1.3 round 1."""

from __future__ import annotations

import math
import hashlib
import random
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import MutableMapping
from zoneinfo import ZoneInfo

from simulator.config import ProfileName, SimulatorConfig
from simulator.identifiers import customer_id as generate_customer_id
from simulator.identifiers import transaction_id
from simulator.randomness import keyed_rng
from simulator.storage import insert_operational_record
from simulator.world import WorldSummary, build_world_summary


LEGITIMATE_NEW_ACCOUNTS_METADATA_KEY = "p1.4.legitimate_new_accounts.v1"


@dataclass(frozen=True)
class TransactionSummary:
    transaction_count: int
    lifecycle_event_count: int
    account_transfer_count: int = 0
    legitimate_new_customer_ids: tuple[str, ...] = ()


def _poisson_count(mean: float, rng: random.Random) -> int:
    """Draw a Poisson count without underflow or a negative zero-mean result."""
    if not math.isfinite(mean) or mean < 0:
        raise ValueError("Poisson mean must be finite and non-negative")
    if mean == 0:
        return 0

    if mean < 30:
        # Knuth's product method in log space. Comparing against exp(-mean)
        # rounds the threshold to 1 for tiny means and underflows to 0 for
        # large means, both of which distort the result.
        log_product = 0.0
        count = 0
        while log_product > -mean:
            count += 1
            uniform = rng.random()
            if uniform == 0:
                return count - 1
            log_product += math.log(uniform)
        return count - 1

    # Hörmann's PTRS transformed rejection method avoids work linear in the
    # mean while retaining a Poisson draw for large values.
    root = math.sqrt(mean)
    b = 0.931 + 2.53 * root
    a = -0.059 + 0.02483 * b
    inverse_alpha = 1.1239 + 1.1328 / (b - 3.4)
    squeeze = 0.9277 - 3.6224 / (b - 2)
    log_mean = math.log(mean)

    while True:
        u = rng.random() - 0.5
        v = rng.random()
        distance = 0.5 - abs(u)
        if distance <= 0:
            continue
        candidate = math.floor((2 * a / distance + b) * u + mean + 0.43)
        if distance >= 0.07 and v <= squeeze:
            return candidate
        if candidate < 0 or (distance < 0.013 and v > distance):
            continue
        lhs = (
            -math.inf
            if v == 0
            else math.log(v * inverse_alpha) - math.log(a / (distance * distance) + b)
        )
        rhs = -mean + candidate * log_mean - math.lgamma(candidate + 1)
        if lhs <= rhs:
            return candidate


def _local_dates(start: date, end_exclusive: date):
    current = start
    while current < end_exclusive:
        yield current
        current += timedelta(days=1)


def _generate_legitimate_new_accounts(
    connection: sqlite3.Connection,
    config: SimulatorConfig,
    *,
    seed: int,
    start_date: date,
    end_date: date,
    merchant_ids: tuple[str, ...],
    transaction_random_keys: MutableMapping[str, str] | None = None,
) -> TransactionSummary:
    """Add ordinary base-history customers with merchant payment history."""
    account_count = config.world.legitimate_new_accounts_per_base_history.value
    if account_count == 0:
        return TransactionSummary(0, 0)
    span_days = (end_date - start_date).days
    if span_days <= 1:
        raise ValueError("legitimate new accounts require a multi-day transaction stream")
    occupied = {
        row[0] for row in connection.execute("SELECT customer_id FROM customers")
    }
    business_tz = ZoneInfo(config.timeline.business_timezone.value)
    inserted_ids: list[str] = []
    lifecycle_events = 0
    for index in range(1, account_count + 1):
        offset = min(index * 7, span_days - 1)
        account_date = start_date + timedelta(days=offset)
        available_at = datetime.combine(
            account_date, time(8, 30), tzinfo=business_tz
        ).astimezone(UTC)
        created_at = datetime.combine(
            account_date, time(9, 0), tzinfo=business_tz
        ).astimezone(UTC)
        customer = generate_customer_id(
            config.seed.value,
            "p1.world.legitimate_new_customer",
            "base",
            index,
            account_date.isoformat(),
            occupied=occupied,
        )
        profile_rng = keyed_rng(
            config.seed.value,
            "p1.world.legitimate_new_customer.profile",
            "base",
            index,
        )
        insert_operational_record(
            connection,
            "customers",
            {
                "customer_id": customer,
                "episode_id": "base",
                "event_at": available_at,
                "available_at": available_at,
                "segment": profile_rng.choice(config.world.customer_segments.value),
                "province": profile_rng.choice(config.world.customer_provinces.value),
                "age_band": profile_rng.choice(config.world.customer_age_bands.value),
            },
        )
        draw_key = f"txn-base-{account_date:%Y%m%d}-{customer}-legitimate-new-account"
        txn_id = transaction_id(
            seed,
            "p1.transactions.legitimate_new_customer_payment",
            "base",
            index,
            account_date.isoformat(),
            customer,
        )
        if transaction_random_keys is not None:
            transaction_random_keys[txn_id] = draw_key
        amount = keyed_rng(
            seed, "p1.transactions.legitimate_new_customer.amount", "base", index
        ).randint(
            config.transactions.minimum_amount.value,
            config.transactions.maximum_amount.value,
        )
        channel_weights = config.transactions.channel_weights.value
        channel = keyed_rng(
            seed, "p1.transactions.legitimate_new_customer.channel", "base", index
        ).choices(
            tuple(channel_weights), weights=tuple(channel_weights.values()), k=1
        )[0]
        merchant_id = keyed_rng(
            seed, "p1.transactions.legitimate_new_customer.merchant", "base", index
        ).choice(merchant_ids)
        lifecycle_events += _insert_transaction_with_lifecycle(
            connection,
            config,
            txn_id=txn_id,
            episode_id="base",
            customer_id=customer,
            merchant_id=merchant_id,
            recipient_customer_id=None,
            created_at=created_at,
            amount=amount,
            channel=channel,
            seed=seed,
            identity_seed=config.seed.value,
            event_boundary=datetime.combine(end_date, time.min).replace(tzinfo=business_tz).astimezone(UTC),
            random_key=draw_key,
        )
        inserted_ids.append(customer)
    return TransactionSummary(
        len(inserted_ids), lifecycle_events, legitimate_new_customer_ids=tuple(inserted_ids)
    )


def card_token_for_customer(seed: int, customer_id: str) -> str:
    """Return a stable opaque token for one customer's synthetic card."""
    digest = hashlib.sha256(f"p1-card-v1\0{seed}\0{customer_id}".encode()).hexdigest()[:20]
    return f"card-{digest}"


def device_id_for_transaction(
    *,
    identity_seed: int,
    draw_seed: int,
    episode_id: str,
    customer_id: str,
    transaction_key: str,
    change_probability: float,
    force_new: bool | None = None,
) -> str:
    """Return a stable primary device or an independently sampled new device."""
    primary_digest = hashlib.sha256(
        f"p1-device-primary-v1\0{identity_seed}\0{customer_id}".encode()
    ).hexdigest()[:20]
    changed = force_new
    if changed is None:
        changed = keyed_rng(
            draw_seed, "p1.devices.legitimate_change", episode_id, transaction_key
        ).random() < change_probability
    if not changed:
        return f"device-{primary_digest}"
    changed_digest = hashlib.sha256(
        f"p1-device-change-v1\0{draw_seed}\0{episode_id}\0{transaction_key}".encode()
    ).hexdigest()[:20]
    return f"device-{changed_digest}"


def _insert_transaction_with_lifecycle(
    connection: sqlite3.Connection,
    config: SimulatorConfig,
    *,
    txn_id: str,
    episode_id: str,
    customer_id: str,
    merchant_id: str | None,
    recipient_customer_id: str | None,
    created_at: datetime,
    amount: int,
    channel: str,
    seed: int,
    identity_seed: int,
    event_boundary: datetime,
    random_key: str | None = None,
) -> int:
    rng_key = txn_id if random_key is None else random_key
    device_id = device_id_for_transaction(
        identity_seed=identity_seed,
        draw_seed=seed,
        episode_id=episode_id,
        customer_id=customer_id,
        transaction_key=rng_key,
        change_probability=config.world.legitimate_device_change_probability.value,
    )
    insert_operational_record(
        connection,
        "transactions",
        {
            "txn_id": txn_id,
            "episode_id": episode_id,
            "customer_id": customer_id,
            "merchant_id": merchant_id,
            "recipient_customer_id": recipient_customer_id,
            "device_id": device_id,
            "card_token": card_token_for_customer(identity_seed, customer_id)
            if channel == "card"
            else None,
            "created_at": created_at,
            "event_at": created_at,
            "available_at": created_at,
            "amount": amount,
            "channel": channel,
        },
    )

    authorization_delay = timedelta(
        seconds=config.transactions.authorization_delay_seconds.value
    )
    settlement_delay = timedelta(
        minutes=config.transactions.settlement_delay_minutes.value
    )
    authorized_at = created_at + authorization_delay
    settled_at = authorized_at + settlement_delay
    for event_type, event_at, response_code in (
        (
            "authorized",
            authorized_at,
            config.transactions.authorization_response_code.value,
        ),
        ("settled", settled_at, None),
    ):
        insert_operational_record(
            connection,
            "transaction_events",
            {
                "event_id": f"{txn_id}:{event_type}",
                "txn_id": txn_id,
                "episode_id": episode_id,
                "event_type": event_type,
                "event_at": event_at,
                "available_at": event_at,
                "amount": None,
                "response_code": response_code,
            },
        )

    draw = keyed_rng(seed, "p1.transactions.adjustment_type", episode_id, rng_key).random()
    refund_probability = config.transactions.refund_probability.value
    reversal_probability = config.transactions.reversal_probability.value
    if draw < refund_probability:
        adjustment_type = "refunded"
    elif draw < refund_probability + reversal_probability:
        adjustment_type = "reversed"
    else:
        adjustment_type = None

    event_count = 2
    if adjustment_type is not None:
        minimum_delay = config.transactions.adjustment_minimum_delay_minutes.value * 60
        maximum_delay = config.transactions.adjustment_maximum_delay_hours.value * 3600
        delay_seconds = keyed_rng(
            seed, "p1.transactions.adjustment_time", episode_id, rng_key
        ).randint(minimum_delay, maximum_delay)
        adjustment_at = settled_at + timedelta(seconds=delay_seconds)
        if adjustment_at < event_boundary:
            adjustment_amount = keyed_rng(
                seed, "p1.transactions.adjustment_amount", episode_id, rng_key
            ).randint(1, amount)
            insert_operational_record(
                connection,
                "transaction_events",
                {
                    "event_id": f"{txn_id}:{adjustment_type}",
                    "txn_id": txn_id,
                    "episode_id": episode_id,
                    "event_type": adjustment_type,
                    "event_at": adjustment_at,
                    "available_at": adjustment_at,
                    "amount": adjustment_amount,
                    "response_code": None,
                },
            )
            event_count += 1

    return event_count


def generate_transactions(
    connection: sqlite3.Connection,
    config: SimulatorConfig,
    profile: ProfileName = "smoke",
    *,
    seed: int | None = None,
    world_summary: WorldSummary | None = None,
    transaction_random_keys: MutableMapping[str, str] | None = None,
) -> TransactionSummary:
    """Insert base merchant payments, A2A transfers, and lifecycle events.

    Counts are sampled independently per customer and Vietnam business date.
    Transaction count, hour, amount, payee, channel, and adjustment use separate
    keyed RNG streams so an added behavior does not consume an unrelated stream.
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
    if world_summary is None:
        world_summary = build_world_summary(
            customer_ids,
            merchant_ids,
            config,
            seed=config.seed.value if seed is None else seed,
        )
    if set(world_summary.customer_ids) != set(customer_ids) or set(
        world_summary.merchant_ids
    ) != set(merchant_ids):
        raise ValueError("world summary does not match generated base entities")

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
    event_boundary = t0_local.astimezone(UTC)
    daily_mean = config.world.expected_transactions_per_customer_local_day.value
    transfer_mean = config.transactions.account_transfer_rate_per_individual_local_day.value
    hourly_weights = config.transactions.hourly_transaction_weights.value
    min_amount = config.transactions.minimum_amount.value
    max_amount = config.transactions.maximum_amount.value
    channel_weights = config.transactions.channel_weights.value
    authorization_delay = timedelta(seconds=config.transactions.authorization_delay_seconds.value)
    settlement_delay = timedelta(minutes=config.transactions.settlement_delay_minutes.value)

    new_account_summary = _generate_legitimate_new_accounts(
        connection,
        config,
        seed=run_seed,
        start_date=start_date,
        end_date=t0_date,
        merchant_ids=merchant_ids,
        transaction_random_keys=transaction_random_keys,
    )
    transaction_count = new_account_summary.transaction_count
    lifecycle_event_count = new_account_summary.lifecycle_event_count
    account_transfer_count = 0
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
                draw_key = (
                    f"txn-base-{business_date:%Y%m%d}-{customer_id}-d{daily_index:04d}"
                )
                txn_id = transaction_id(
                    run_seed,
                    "p1.transactions.merchant_payment",
                    "base",
                    business_date.isoformat(),
                    customer_id,
                    daily_index,
                )
                if transaction_random_keys is not None:
                    transaction_random_keys[txn_id] = draw_key
                time_rng = keyed_rng(run_seed, "p1.transactions.time", draw_key)
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

                merchant_candidates = world_summary.supplier_merchants.get(
                    customer_id, merchant_ids
                )
                merchant_rng = keyed_rng(run_seed, "p1.transactions.merchant", draw_key)
                amount_rng = keyed_rng(run_seed, "p1.transactions.amount", draw_key)
                channel_rng = keyed_rng(run_seed, "p1.transactions.channel", draw_key)
                merchant_id = merchant_rng.choice(merchant_candidates)
                amount = amount_rng.randint(min_amount, max_amount)
                channel = channel_rng.choices(
                    tuple(channel_weights),
                    weights=tuple(channel_weights.values()),
                    k=1,
                )[0]

                lifecycle_event_count += _insert_transaction_with_lifecycle(
                    connection,
                    config,
                    txn_id=txn_id,
                    episode_id="base",
                    customer_id=customer_id,
                    merchant_id=merchant_id,
                    recipient_customer_id=None,
                    created_at=created_at_utc,
                    amount=amount,
                    channel=channel,
                    seed=run_seed,
                    identity_seed=run_seed,
                    event_boundary=event_boundary,
                    random_key=draw_key,
                )
                transaction_count += 1

            if world_summary.account_roles[customer_id] != "individual":
                continue
            payees = world_summary.familiar_payees.get(customer_id, ())
            if not payees:
                continue
            transfer_count = _poisson_count(
                transfer_mean,
                keyed_rng(
                    run_seed,
                    "p1.transactions.account_transfer_count",
                    business_date.isoformat(),
                    customer_id,
                ),
            )
            for transfer_index in range(1, transfer_count + 1):
                daily_index = count + transfer_index
                draw_key = (
                    f"txn-base-{business_date:%Y%m%d}-{customer_id}-d{daily_index:04d}"
                )
                txn_id = transaction_id(
                    run_seed,
                    "p1.transactions.account_transfer",
                    "base",
                    business_date.isoformat(),
                    customer_id,
                    transfer_index,
                )
                if transaction_random_keys is not None:
                    transaction_random_keys[txn_id] = draw_key
                time_rng = keyed_rng(run_seed, "p1.transactions.a2a_time", draw_key)
                hour = time_rng.choices(range(24), weights=hourly_weights, k=1)[0]
                local_created_at = datetime.combine(
                    business_date,
                    time(hour, time_rng.randrange(60), time_rng.randrange(60)),
                ).replace(tzinfo=business_tz)
                created_at_utc = local_created_at.astimezone(UTC)
                if created_at_utc > latest_creation_utc:
                    continue
                recipient_id = keyed_rng(
                    run_seed, "p1.transactions.a2a_payee", draw_key
                ).choice(payees)
                amount = keyed_rng(
                    run_seed, "p1.transactions.a2a_amount", draw_key
                ).randint(min_amount, max_amount)
                lifecycle_event_count += _insert_transaction_with_lifecycle(
                    connection,
                    config,
                    txn_id=txn_id,
                    episode_id="base",
                    customer_id=customer_id,
                    merchant_id=None,
                    recipient_customer_id=recipient_id,
                    created_at=created_at_utc,
                    amount=amount,
                    channel=config.transactions.account_transfer_channel.value,
                    seed=run_seed,
                    identity_seed=run_seed,
                    event_boundary=event_boundary,
                    random_key=draw_key,
                )
                transaction_count += 1
                account_transfer_count += 1

    return TransactionSummary(
        transaction_count,
        lifecycle_event_count,
        account_transfer_count,
        new_account_summary.legitimate_new_customer_ids,
    )


def generate_episode_transactions(
    connection: sqlite3.Connection,
    config: SimulatorConfig,
    profile: ProfileName,
    *,
    episode_id: str,
    seed: int,
    duration_days: int,
    world_summary: WorldSummary,
    transaction_random_keys: MutableMapping[str, str] | None = None,
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
    customer_ids = tuple(world_summary.customer_ids)
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
    if set(world_summary.customer_ids) != set(customer_ids) or set(
        world_summary.merchant_ids
    ) != set(merchant_ids):
        raise ValueError("world summary does not match generated base entities")

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
    transfer_mean = config.transactions.account_transfer_rate_per_individual_local_day.value
    hourly_weights = config.transactions.hourly_transaction_weights.value
    authorization_delay = timedelta(seconds=config.transactions.authorization_delay_seconds.value)
    settlement_delay = timedelta(minutes=config.transactions.settlement_delay_minutes.value)
    transaction_count = 0
    lifecycle_event_count = 0
    account_transfer_count = 0
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
                draw_key = (
                    f"txn-{episode_id}-{business_date:%Y%m%d}-{customer_id}-d{daily_index:04d}"
                )
                txn_id = transaction_id(
                    seed,
                    "p1.7.transactions.merchant_payment",
                    episode_id,
                    business_date.isoformat(),
                    customer_id,
                    daily_index,
                )
                if transaction_random_keys is not None:
                    transaction_random_keys[txn_id] = draw_key
                time_rng = keyed_rng(seed, "p1.7.transactions.time", episode_id, draw_key)
                hour = time_rng.choices(range(24), weights=hourly_weights, k=1)[0]
                local_created_at = datetime.combine(
                    business_date,
                    time(hour, time_rng.randrange(60), time_rng.randrange(60)),
                ).replace(tzinfo=business_tz)
                created_at = local_created_at.astimezone(UTC)
                if created_at > latest_creation_utc:
                    continue

                merchant_candidates = world_summary.supplier_merchants.get(
                    customer_id, merchant_ids
                )
                merchant_id = keyed_rng(
                    seed, "p1.7.transactions.merchant", episode_id, draw_key
                ).choice(merchant_candidates)
                channel_weights = config.transactions.channel_weights.value
                channel = keyed_rng(
                    seed, "p1.7.transactions.channel", episode_id, draw_key
                ).choices(
                    tuple(channel_weights),
                    weights=tuple(channel_weights.values()),
                    k=1,
                )[0]
                amount = keyed_rng(
                    seed, "p1.7.transactions.amount", episode_id, draw_key
                ).randint(
                    config.transactions.minimum_amount.value,
                    config.transactions.maximum_amount.value,
                )
                lifecycle_event_count += _insert_transaction_with_lifecycle(
                    connection,
                    config,
                    txn_id=txn_id,
                    episode_id=episode_id,
                    customer_id=customer_id,
                    merchant_id=merchant_id,
                    recipient_customer_id=None,
                    created_at=created_at,
                    amount=amount,
                    channel=channel,
                    seed=seed,
                    identity_seed=config.seed.value,
                    event_boundary=end_utc,
                    random_key=draw_key,
                )
                transaction_count += 1

            if world_summary.account_roles[customer_id] != "individual":
                continue
            payees = world_summary.familiar_payees.get(customer_id, ())
            if not payees:
                continue
            transfer_count = _poisson_count(
                transfer_mean,
                keyed_rng(
                    seed,
                    "p1.7.transactions.account_transfer_count",
                    episode_id,
                    business_date.isoformat(),
                    customer_id,
                ),
            )
            for transfer_index in range(1, transfer_count + 1):
                daily_index = count + transfer_index
                draw_key = (
                    f"txn-{episode_id}-{business_date:%Y%m%d}-{customer_id}-d{daily_index:04d}"
                )
                txn_id = transaction_id(
                    seed,
                    "p1.7.transactions.account_transfer",
                    episode_id,
                    business_date.isoformat(),
                    customer_id,
                    transfer_index,
                )
                if transaction_random_keys is not None:
                    transaction_random_keys[txn_id] = draw_key
                time_rng = keyed_rng(
                    seed, "p1.7.transactions.a2a_time", episode_id, draw_key
                )
                hour = time_rng.choices(range(24), weights=hourly_weights, k=1)[0]
                local_created_at = datetime.combine(
                    business_date,
                    time(hour, time_rng.randrange(60), time_rng.randrange(60)),
                ).replace(tzinfo=business_tz)
                created_at = local_created_at.astimezone(UTC)
                if created_at > latest_creation_utc:
                    continue
                recipient_id = keyed_rng(
                    seed, "p1.7.transactions.a2a_payee", episode_id, draw_key
                ).choice(payees)
                amount = keyed_rng(
                    seed, "p1.7.transactions.a2a_amount", episode_id, draw_key
                ).randint(
                    config.transactions.minimum_amount.value,
                    config.transactions.maximum_amount.value,
                )
                lifecycle_event_count += _insert_transaction_with_lifecycle(
                    connection,
                    config,
                    txn_id=txn_id,
                    episode_id=episode_id,
                    customer_id=customer_id,
                    merchant_id=None,
                    recipient_customer_id=recipient_id,
                    created_at=created_at,
                    amount=amount,
                    channel=config.transactions.account_transfer_channel.value,
                    seed=seed,
                    identity_seed=config.seed.value,
                    event_boundary=end_utc,
                    random_key=draw_key,
                )
                transaction_count += 1
                account_transfer_count += 1

    return TransactionSummary(
        transaction_count,
        lifecycle_event_count,
        account_transfer_count,
    )
