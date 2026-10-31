"""Generate the P1.4 card-testing mechanism and benign comparison traffic."""

from __future__ import annotations

import sqlite3
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from typing import MutableMapping
from zoneinfo import ZoneInfo

from simulator.config import SimulatorConfig
from simulator.identifiers import customer_id as generate_customer_id
from simulator.identifiers import transaction_id
from simulator.randomness import keyed_rng
from simulator.storage import insert_operational_record, insert_private_record
from simulator.transactions import card_token_for_customer, device_id_for_transaction
from simulator.world import WorldSummary


CARD_TESTING_METADATA_KEY = "p1.4.card_testing.v1"
MECHANISM_METADATA_PREFIX = "p1.4.mechanisms.v1"


@dataclass(frozen=True)
class FraudSummary:
    campaign_count: int
    small_attempt_count: int
    approved_small_attempt_count: int
    declined_small_attempt_count: int
    larger_followup_count: int
    approved_larger_followup_count: int
    benign_control_transaction_count: int
    approved_benign_control_count: int
    declined_benign_control_count: int
    transaction_count: int
    lifecycle_event_count: int


def _campaign_dates(start_date: date, t0_date: date, interval_days: int) -> list[date]:
    dates: list[date] = []
    current = start_date
    while current < t0_date:
        dates.append(current)
        current += timedelta(days=interval_days)
    return dates


def _fraud_transaction_id(
    seed: int,
    namespace: str,
    episode_id: str,
    *keys: object,
    occupied: set[str],
    transaction_random_keys: MutableMapping[str, str] | None = None,
) -> str:
    """Return a namespaced, opaque ID independent of background transaction counts."""
    scope = "base" if episode_id == "base" else episode_id
    legacy_payload = json.dumps(
        ["payguard-fraud-txn-v1", seed, namespace, episode_id, *keys],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    legacy_random_key = f"txn-{scope}-x{hashlib.sha256(legacy_payload).hexdigest()[:28]}"
    txn_id = transaction_id(seed, namespace, episode_id, *keys)
    if txn_id in occupied:
        raise ValueError("fraud transaction ID collision in a namespaced stream")
    occupied.add(txn_id)
    if transaction_random_keys is not None:
        transaction_random_keys[txn_id] = legacy_random_key
    return txn_id


def _insert_transaction(
    operational: sqlite3.Connection,
    *,
    txn_id: str,
    episode_id: str,
    event_ids: set[str],
    customer_id: str,
    merchant_id: str | None,
    recipient_customer_id: str | None = None,
    channel: str,
    amount: int,
    created_at: datetime,
    device_id: str,
    card_token: str | None = None,
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
            "episode_id": episode_id,
            "customer_id": customer_id,
            "merchant_id": merchant_id,
            "recipient_customer_id": recipient_customer_id,
            "device_id": device_id,
            "card_token": card_token,
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
            "episode_id": episode_id,
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
            "episode_id": episode_id,
            "event_type": "settled",
            "event_at": settled_at,
            "available_at": settled_at,
            "response_code": None,
        },
    )
    return 2


def card_testing_metadata_key(episode_id: str) -> str:
    """Return the private metadata key for one history or episode stream."""
    if episode_id == "base":
        return CARD_TESTING_METADATA_KEY
    return f"{CARD_TESTING_METADATA_KEY}:{episode_id}"


def mechanism_metadata_key(episode_id: str) -> str:
    """Return the private metadata key for non-card P1.4 mechanisms."""
    if episode_id == "base":
        return MECHANISM_METADATA_PREFIX
    return f"{MECHANISM_METADATA_PREFIX}:{episode_id}"


def generate_card_testing(
    operational: sqlite3.Connection,
    private: sqlite3.Connection,
    config: SimulatorConfig,
    *,
    seed: int | None = None,
    episode_id: str = "base",
    duration_days: int = 30,
    customer_ids: tuple[str, ...] | None = None,
    transaction_random_keys: MutableMapping[str, str] | None = None,
) -> FraudSummary:
    """Append probabilistic card testing and similar benign traffic to one stream.

    Card attempts and their later larger transactions use channel ``card``. Each
    small attempt and larger follow-up has an independent keyed approval draw.
    A benign control emits the same number and timing of small card transactions,
    all successfully authorized and settled, with no larger follow-up.

    Mechanism, role, seed, and entity-to-pattern membership are stored only in
    the private ``run_metadata`` export. Operational IDs do not encode a fraud
    mechanism or label; episode transaction IDs use an episode-specific prefix.
    """
    if operational is private:
        raise ValueError("fraud generation requires separate operational and private connections")

    mechanism = config.fraud.card_testing
    run_seed = config.seed.value if seed is None else seed
    if isinstance(run_seed, bool) or not isinstance(run_seed, int) or run_seed < 0:
        raise ValueError("fraud seed must be a non-negative integer")
    if not isinstance(episode_id, str) or not episode_id.strip():
        raise ValueError("fraud generation requires a non-empty episode_id")
    if isinstance(duration_days, bool) or not isinstance(duration_days, int) or duration_days <= 0:
        raise ValueError("duration_days must be a positive integer")
    if episode_id != "base":
        if operational.execute(
            "SELECT 1 FROM episodes WHERE episode_id = ? AND role <> 'base'", (episode_id,)
        ).fetchone() is None:
            raise ValueError("card-testing generation requires a registered episode")
        if private.execute(
            "SELECT 1 FROM private_episodes WHERE episode_id = ?", (episode_id,)
        ).fetchone() is None:
            raise ValueError("card-testing episode must be registered in private storage")
    elif operational.execute(
        "SELECT 1 FROM episodes WHERE episode_id = 'base' AND role = 'base'"
    ).fetchone() is None:
        raise ValueError("base episode must be registered before card-testing generation")
    metadata_key = card_testing_metadata_key(episode_id)
    if private.execute(
        "SELECT 1 FROM run_metadata WHERE key = ?", (metadata_key,)
    ).fetchone():
        raise ValueError("card-testing generation has already been recorded in this private export")

    if customer_ids is None:
        customer_ids = tuple(
            row[0]
            for row in operational.execute(
                "SELECT customer_id FROM customers WHERE episode_id IN ('base', ?) ORDER BY customer_id",
                (episode_id,),
            )
        )
    else:
        customer_ids = tuple(sorted(customer_ids))
    merchant_ids = [
        row[0]
        for row in operational.execute(
            "SELECT merchant_id FROM merchants WHERE episode_id IN ('base', ?) ORDER BY merchant_id",
            (episode_id,),
        )
    ]
    if not customer_ids:
        raise ValueError("generate_world must create base customers before fraud generation")
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

    business_timezone = ZoneInfo(config.timeline.business_timezone.value)
    start_date = config.timeline.start_business_date.value
    t0 = config.timeline.t0_business_date.value
    if episode_id == "base":
        campaign_start = start_date
        campaign_end = t0
    else:
        campaign_start = t0
        campaign_end = t0 + timedelta(days=duration_days)
    campaign_dates = _campaign_dates(
        campaign_start, campaign_end, mechanism.campaign_interval_days.value
    )
    if not campaign_dates:
        raise ValueError("card-testing generation requires at least one campaign date")
    campaign_count = len(campaign_dates)
    control_date = t0 - timedelta(days=1) if episode_id == "base" else campaign_start
    scope = () if episode_id == "base" else (episode_id,)
    control_customer = keyed_rng(
        run_seed, "p1.fraud.benign_control.customer", *scope
    ).choice(customer_ids)
    control_merchant = keyed_rng(
        run_seed, "p1.fraud.benign_control.merchant", *scope
    ).choice(merchant_ids)
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
    approved_benign_control_count = 0
    declined_benign_control_count = 0
    transaction_count = 0
    lifecycle_event_count = 0

    operational.execute("SAVEPOINT p1_4_card_testing")
    private.execute("SAVEPOINT p1_4_card_testing_metadata")
    try:
        for pattern_index, campaign_date in enumerate(campaign_dates):
            victim_rng = keyed_rng(
                run_seed,
                "p1.fraud.card_testing.victim",
                *scope,
                campaign_date.isoformat(),
            )
            campaign_customer = victim_rng.choice(customer_ids)
            campaign_merchant = keyed_rng(
                run_seed,
                "p1.fraud.card_testing.merchant",
                *scope,
                campaign_date.isoformat(),
            ).choice(merchant_ids)
            for role in ("fraud", "benign_control"):
                if role == "benign_control" and pattern_index != 0:
                    continue
                if role == "fraud":
                    group_date = campaign_date
                    customer_id = campaign_customer
                    merchant_id = campaign_merchant
                else:
                    group_date = control_date
                    customer_id = control_customer
                    merchant_id = control_merchant
                component = (
                    "p1.fraud.card_testing"
                    if role == "fraud"
                    else "p1.fraud.benign_control"
                )
                group_start = datetime.combine(
                    group_date,
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
                        run_seed,
                        f"{component}.amount",
                        *scope,
                        pattern_index,
                        "small",
                        attempt_index,
                    )
                    amount = amount_rng.randint(
                        mechanism.minimum_attempt_amount.value,
                        mechanism.maximum_attempt_amount.value,
                    )
                    txn_id = _fraud_transaction_id(
                        run_seed,
                        "p1.fraud.card_testing.txn_id",
                        episode_id,
                        pattern_index,
                        role,
                        "small",
                        attempt_index,
                        occupied=txn_ids,
                        transaction_random_keys=transaction_random_keys,
                    )
                    if role == "fraud":
                        outcome_rng = keyed_rng(
                            run_seed,
                            "p1.fraud.card_testing.approval",
                            *scope,
                            pattern_index,
                            attempt_index,
                        )
                        approved = outcome_rng.random() < mechanism.approval_probability.value
                    else:
                        approved = True

                    group_event_count += _insert_transaction(
                        operational,
                        txn_id=txn_id,
                        episode_id=episode_id,
                        event_ids=event_ids,
                        customer_id=customer_id,
                        merchant_id=merchant_id,
                        recipient_customer_id=None,
                        channel=mechanism.channel.value,
                        amount=amount,
                        created_at=created_at,
                        device_id=device_id_for_transaction(
                            identity_seed=config.seed.value,
                            draw_seed=run_seed,
                            episode_id=episode_id,
                            customer_id=customer_id,
                            transaction_key=(
                                f"card-testing/{episode_id}/{pattern_index}/{role}/small/{attempt_index}"
                            ),
                            change_probability=0.0,
                            force_new=False,
                        ),
                        card_token=card_token_for_customer(config.seed.value, customer_id),
                        authorization_delay=authorization_delay,
                        settlement_delay=settlement_delay,
                        approved=approved,
                        authorization_response_code=authorization_response_code,
                    )
                    small_ids.append(txn_id)
                    transaction_count += 1
                    if approved:
                        group_approved_small += 1
                    else:
                        group_declined_small += 1
                    if role == "fraud":
                        small_attempt_count += 1
                        if approved:
                            approved_small_attempt_count += 1
                        else:
                            declined_small_attempt_count += 1
                    else:
                        benign_control_transaction_count += 1
                        if approved:
                            approved_benign_control_count += 1
                        else:
                            declined_benign_control_count += 1

                metadata: dict[str, object] = {
                    "role": role,
                    "pattern_id": f"card-testing-{pattern_index:04d}-{role}",
                    "business_date": group_date.isoformat(),
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
                        run_seed,
                        "p1.fraud.card_testing.amount",
                        *scope,
                        pattern_index,
                        "followup",
                    )
                    followup_amount = followup_amount_rng.randint(
                        mechanism.minimum_followup_amount.value,
                        mechanism.maximum_followup_amount.value,
                    )
                    followup_txn_id = _fraud_transaction_id(
                        run_seed,
                        "p1.fraud.card_testing.txn_id",
                        episode_id,
                        pattern_index,
                        "fraud",
                        "followup",
                        occupied=txn_ids,
                        transaction_random_keys=transaction_random_keys,
                    )
                    followup_outcome_rng = keyed_rng(
                        run_seed,
                        "p1.fraud.card_testing.approval",
                        *scope,
                        pattern_index,
                        "followup",
                    )
                    followup_approved = (
                        followup_outcome_rng.random()
                        < mechanism.followup_approval_probability.value
                    )
                    followup_events = _insert_transaction(
                        operational,
                        txn_id=followup_txn_id,
                        episode_id=episode_id,
                        event_ids=event_ids,
                        customer_id=customer_id,
                        merchant_id=merchant_id,
                        recipient_customer_id=None,
                        channel=mechanism.channel.value,
                        amount=followup_amount,
                        created_at=followup_created_at,
                        device_id=device_id_for_transaction(
                            identity_seed=config.seed.value,
                            draw_seed=run_seed,
                            episode_id=episode_id,
                            customer_id=customer_id,
                            transaction_key=(
                                f"card-testing/{episode_id}/{pattern_index}/fraud/followup"
                            ),
                            change_probability=0.0,
                            force_new=False,
                        ),
                        card_token=card_token_for_customer(config.seed.value, customer_id),
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
            campaign_count=campaign_count,
            small_attempt_count=small_attempt_count,
            approved_small_attempt_count=approved_small_attempt_count,
            declined_small_attempt_count=declined_small_attempt_count,
            larger_followup_count=larger_followup_count,
            approved_larger_followup_count=approved_larger_followup_count,
            benign_control_transaction_count=benign_control_transaction_count,
            approved_benign_control_count=approved_benign_control_count,
            declined_benign_control_count=declined_benign_control_count,
            transaction_count=transaction_count,
            lifecycle_event_count=lifecycle_event_count,
        )
        insert_private_record(
            private,
            "run_metadata",
            {
                "key": metadata_key,
                "value_json": {
                    "algorithm": "p1.4-card-testing-v2",
                    "episode_id": episode_id,
                    "seed": run_seed,
                    "mechanism": "card_testing",
                    "channel": mechanism.channel.value,
                    "campaign_interval_days": mechanism.campaign_interval_days.value,
                    "duration_days": duration_days if episode_id != "base" else None,
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


def generate_other_fraud_mechanisms(
    operational: sqlite3.Connection,
    private: sqlite3.Connection,
    config: SimulatorConfig,
    world_summary: WorldSummary,
    *,
    seed: int,
    episode_id: str,
    duration_days: int = 30,
    include_episode_mechanisms: bool = True,
    transaction_random_keys: MutableMapping[str, str] | None = None,
) -> dict[str, int]:
    """Generate ATO, pre-drift collusion, social-engineering, and benign controls.

    Operational rows contain only transaction/customer attributes. Mechanism,
    role, group membership, and truth IDs stay in private run metadata.
    """
    if operational is private:
        raise ValueError("fraud generation requires separate operational and private connections")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("fraud seed must be a non-negative integer")
    if not isinstance(episode_id, str) or not episode_id.strip():
        raise ValueError("fraud generation requires a non-empty episode_id")
    if isinstance(duration_days, bool) or not isinstance(duration_days, int) or duration_days <= 0:
        raise ValueError("duration_days must be a positive integer")
    if not isinstance(include_episode_mechanisms, bool):
        raise ValueError("include_episode_mechanisms must be a boolean")
    if episode_id == "base":
        registered = operational.execute(
            "SELECT 1 FROM episodes WHERE episode_id = 'base' AND role = 'base'"
        ).fetchone()
    else:
        registered = operational.execute(
            "SELECT 1 FROM episodes WHERE episode_id = ? AND role <> 'base'", (episode_id,)
        ).fetchone()
        if private.execute(
            "SELECT 1 FROM private_episodes WHERE episode_id = ?", (episode_id,)
        ).fetchone() is None:
            raise ValueError("fraud episode must be registered in private storage")
    if registered is None:
        raise ValueError("fraud generation requires a registered episode")
    metadata_key = mechanism_metadata_key(episode_id)
    if private.execute(
        "SELECT 1 FROM run_metadata WHERE key = ?", (metadata_key,)
    ).fetchone():
        raise ValueError("P1.4 mechanism generation has already been recorded in this private export")

    if episode_id == "base":
        start_date = config.timeline.start_business_date.value
        end_date = config.timeline.t0_business_date.value
    else:
        start_date = config.timeline.t0_business_date.value
        end_date = start_date + timedelta(days=duration_days)
    business_tz = ZoneInfo(config.timeline.business_timezone.value)
    scope = () if episode_id == "base" else (episode_id,)
    identity_seed = config.seed.value
    roles = world_summary.account_roles
    individuals = tuple(sorted(key for key, value in roles.items() if value == "individual"))
    mules = tuple(sorted(key for key, value in roles.items() if value == "internal_mule"))
    households = tuple(sorted(key for key, value in roles.items() if value == "household_business"))
    older_individuals = tuple(
        row[0]
        for row in operational.execute(
            "SELECT customer_id FROM customers WHERE customer_id IN ("
            + ",".join("?" for _ in individuals)
            + ") AND age_band = '55+' ORDER BY customer_id",
            individuals,
        )
    ) if individuals else ()
    merchant_ids = tuple(row[0] for row in operational.execute(
        "SELECT merchant_id FROM merchants WHERE episode_id IN ('base', ?) ORDER BY merchant_id",
        (episode_id,),
    ))
    if not merchant_ids:
        raise ValueError("generate_world must create base merchants before fraud generation")

    occupied_txn_ids = {row[0] for row in operational.execute("SELECT txn_id FROM transactions")}
    occupied_customer_ids = {
        row[0] for row in operational.execute("SELECT customer_id FROM customers")
    }
    event_ids = {row[0] for row in operational.execute("SELECT event_id FROM transaction_events")}
    amount_min = config.fraud.near_threshold_minimum_amount.value
    amount_max = config.fraud.near_threshold_maximum_amount.value
    authorization_delay = timedelta(seconds=config.transactions.authorization_delay_seconds.value)
    settlement_delay = timedelta(minutes=config.transactions.settlement_delay_minutes.value)
    response_code = config.transactions.authorization_response_code.value
    categories = {
        "segment": config.world.customer_segments.value,
        "province": config.world.customer_provinces.value,
        "age_band": config.world.customer_age_bands.value,
    }
    groups: list[dict[str, object]] = []
    summary = {
        "ato_transaction_count": 0,
        "collusion_transaction_count": 0,
        "social_engineering_transaction_count": 0,
        "business_control_transaction_count": 0,
        "lifecycle_event_count": 0,
    }

    def local_start(day: date, hour: int, minute_offset: int = 0) -> datetime:
        return datetime.combine(day, time(hour, 0), tzinfo=business_tz) + timedelta(minutes=minute_offset)

    def new_customer_id(namespace: str, day: date, index: int) -> str:
        return generate_customer_id(
            seed,
            f"p1.fraud.{namespace}.customer_id",
            episode_id,
            day.isoformat(),
            index,
            occupied=occupied_customer_ids,
        )

    def write_transaction(
        *,
        namespace: str,
        group_index: int,
        transaction_index: int,
        sender_id: str,
        recipient_id: str | None,
        merchant_id: str | None,
        channel: str,
        amount: int,
        created_at: datetime,
        device_id: str,
        approved: bool = True,
    ) -> tuple[str, int]:
        txn_id = _fraud_transaction_id(
            seed,
            f"p1.fraud.{namespace}.txn_id",
            episode_id,
            group_index,
            transaction_index,
            occupied=occupied_txn_ids,
            transaction_random_keys=transaction_random_keys,
        )
        event_count = _insert_transaction(
            operational,
            txn_id=txn_id,
            episode_id=episode_id,
            event_ids=event_ids,
            customer_id=sender_id,
            merchant_id=merchant_id,
            recipient_customer_id=recipient_id,
            channel=channel,
            amount=amount,
            created_at=created_at,
            device_id=device_id,
            card_token=card_token_for_customer(identity_seed, sender_id) if channel == "card" else None,
            authorization_delay=authorization_delay,
            settlement_delay=settlement_delay,
            approved=approved,
            authorization_response_code=response_code,
        )
        return txn_id, event_count

    def add_new_mule(
        day: date, group_index: int, hour: int, *, warmup_days: int
    ) -> str:
        customer_id = new_customer_id("recipient", day, group_index)
        if operational.execute(
            "SELECT 1 FROM customers WHERE customer_id = ?", (customer_id,)
        ).fetchone():
            raise ValueError("P1.4 generated customer ID collision")
        account_start = local_start(day, hour) - timedelta(days=warmup_days, minutes=1)
        profile_rng = keyed_rng(
            seed, "p1.fraud.social_engineering.new_account_profile", *scope, group_index
        )
        insert_operational_record(
            operational,
            "customers",
            {
                "customer_id": customer_id,
                "episode_id": episode_id,
                "event_at": account_start,
                "available_at": account_start,
                "segment": profile_rng.choice(categories["segment"]),
                "province": profile_rng.choice(categories["province"]),
                "age_band": profile_rng.choice(categories["age_band"]),
            },
        )
        return customer_id

    operational.execute("SAVEPOINT p1_4_mechanisms")
    private.execute("SAVEPOINT p1_4_mechanisms_metadata")
    try:
        # ATO carries a probabilistic new-device signature. Independently
        # sampled legitimate device changes already occur in normal traffic.
        ato = config.fraud.ato
        if individuals and (episode_id == "base" or include_episode_mechanisms):
            ato_start = start_date + timedelta(days=ato.start_offset_days.value)
            ato_span = timedelta(
                days=(ato.transaction_count.value - 1) * ato.transaction_spacing_days.value
            )
            for group_index, campaign_date in enumerate(
                campaign_date
                for campaign_date in _campaign_dates(
                    ato_start, end_date, ato.campaign_interval_days.value
                )
                if campaign_date + ato_span < end_date
            ):
                target = keyed_rng(
                    seed, "p1.fraud.ato.target", *scope, campaign_date.isoformat()
                ).choice(individuals)
                merchant_id = keyed_rng(
                    seed, "p1.fraud.ato.merchant", *scope, campaign_date.isoformat()
                ).choice(merchant_ids)
                has_signature = keyed_rng(
                    seed, "p1.fraud.ato.device_signature", *scope, campaign_date.isoformat()
                ).random() < ato.device_signature_probability.value
                pattern_device = device_id_for_transaction(
                    identity_seed=identity_seed,
                    draw_seed=seed,
                    episode_id=episode_id,
                    customer_id=target,
                    transaction_key=f"ato-pattern-{campaign_date.isoformat()}",
                    change_probability=0.0,
                    force_new=has_signature,
                )
                transaction_ids: list[str] = []
                amounts: list[int] = []
                group_events = 0
                for index in range(ato.transaction_count.value):
                    created_at = local_start(campaign_date, ato.start_hour_local.value) + timedelta(
                        days=index * ato.transaction_spacing_days.value
                    )
                    amount = keyed_rng(
                        seed, "p1.fraud.ato.amount", *scope, campaign_date.isoformat(), index
                    ).randint(amount_min, amount_max)
                    amounts.append(amount)
                    approved = keyed_rng(
                        seed, "p1.fraud.ato.approval", *scope, campaign_date.isoformat(), index
                    ).random() < ato.approval_probability.value
                    txn_id, event_count = write_transaction(
                        namespace="ato", group_index=group_index, transaction_index=index,
                        sender_id=target, recipient_id=None, merchant_id=merchant_id,
                        channel=ato.channel.value, amount=amount, created_at=created_at,
                        device_id=pattern_device, approved=approved,
                    )
                    transaction_ids.append(txn_id)
                    group_events += event_count
                start_at = local_start(campaign_date, ato.start_hour_local.value)
                groups.append({
                    "mechanism": "ato", "role": "fraud",
                    "group_id": f"p14-ato-{group_index:04d}",
                    "customer_ids": [target], "transaction_ids": transaction_ids,
                    "amounts": amounts,
                    "device_signature": has_signature, "start_at": start_at.isoformat(),
                    "end_at": (start_at + ato_span).isoformat(),
                })
                summary["ato_transaction_count"] += len(transaction_ids)
                summary["lifecycle_event_count"] += group_events

        # Collusion is generated only in the pre-drift portion of shared history.
        collusion = config.fraud.collusion
        if episode_id == "base" and individuals and mules:
            pre_drift_end = min(end_date, config.timeline.drift_start_business_date.value)
            for group_index, campaign_date in enumerate(
                _campaign_dates(start_date, pre_drift_end, collusion.campaign_interval_days.value)
            ):
                participant_count = min(collusion.sender_count.value, len(individuals))
                participants = keyed_rng(
                    seed, "p1.fraud.collusion.senders", campaign_date.isoformat()
                ).sample(individuals, participant_count)
                mule_id = keyed_rng(
                    seed, "p1.fraud.collusion.mule", campaign_date.isoformat()
                ).choice(mules)
                cashout_candidates = tuple(item for item in individuals if item not in participants)
                cashout_id = keyed_rng(
                    seed, "p1.fraud.collusion.cashout", campaign_date.isoformat()
                ).choice(cashout_candidates or participants)
                starts = local_start(campaign_date, collusion.start_hour_local.value)
                inbound_ids: list[str] = []
                outbound_ids: list[str] = []
                amounts: list[int] = []
                group_events = 0
                for index, sender_id in enumerate(participants):
                    amount = keyed_rng(
                        seed, "p1.fraud.collusion.amount", campaign_date.isoformat(), index
                    ).randint(amount_min, amount_max)
                    amounts.append(amount)
                    incoming_at = starts + timedelta(minutes=index * collusion.transfer_spacing_minutes.value)
                    device_id = device_id_for_transaction(
                        identity_seed=identity_seed, draw_seed=seed, episode_id=episode_id,
                        customer_id=sender_id, transaction_key=f"collusion-in-{campaign_date}-{index}",
                        change_probability=0.0, force_new=False,
                    )
                    txn_id, event_count = write_transaction(
                        namespace="collusion", group_index=group_index, transaction_index=index,
                        sender_id=sender_id, recipient_id=mule_id, merchant_id=None,
                        channel="wallet", amount=amount, created_at=incoming_at,
                        device_id=device_id,
                    )
                    inbound_ids.append(txn_id)
                    group_events += event_count
                for index, amount in enumerate(amounts):
                    outgoing_at = starts + timedelta(
                        minutes=index * collusion.transfer_spacing_minutes.value
                        + collusion.cashout_delay_minutes.value
                    )
                    device_id = device_id_for_transaction(
                        identity_seed=identity_seed, draw_seed=seed, episode_id=episode_id,
                        customer_id=mule_id, transaction_key=f"collusion-out-{campaign_date}-{index}",
                        change_probability=0.0, force_new=False,
                    )
                    txn_id, event_count = write_transaction(
                        namespace="collusion", group_index=group_index,
                        transaction_index=collusion.sender_count.value + index,
                        sender_id=mule_id, recipient_id=cashout_id, merchant_id=None,
                        channel="wallet", amount=amount, created_at=outgoing_at,
                        device_id=device_id,
                    )
                    outbound_ids.append(txn_id)
                    group_events += event_count
                groups.append({
                    "mechanism": "collusion", "role": "fraud",
                    "group_id": f"p14-collusion-{group_index:04d}",
                    "member_customer_ids": list(participants), "mule_customer_id": mule_id,
                    "cashout_customer_id": cashout_id,
                    "incoming_transaction_ids": inbound_ids,
                    "outgoing_transaction_ids": outbound_ids, "amounts": amounts,
                    "start_at": starts.isoformat(),
                    "end_at": (starts + timedelta(minutes=(len(participants) - 1) * collusion.transfer_spacing_minutes.value + collusion.cashout_delay_minutes.value)).isoformat(),
                })
                summary["collusion_transaction_count"] += len(inbound_ids) + len(outbound_ids)
                summary["lifecycle_event_count"] += group_events

        # Social engineering sends funds to a newly available mule account. A
        # household business receives the same sender count as a benign control.
        social = config.fraud.social_engineering
        if (episode_id == "base" or include_episode_mechanisms) and len(individuals) >= 2 and households:
            fan_in = min(social.fan_in_count.value, len(individuals))
            campaign_span = timedelta(days=(fan_in - 1) * social.incoming_spacing_days.value)
            social_start = start_date + timedelta(days=social.start_offset_days.value)
            for group_index, campaign_date in enumerate(
                campaign_date
                for campaign_date in _campaign_dates(
                    social_start, end_date, social.campaign_interval_days.value
                )
                if campaign_date + campaign_span < end_date
            ):
                if fan_in < 2:
                    continue
                starts = local_start(campaign_date, social.start_hour_local.value)
                control_at = starts + campaign_span + timedelta(hours=2)
                control_at_text = control_at.astimezone(ZoneInfo("UTC")).isoformat(timespec="microseconds").replace("+00:00", "Z")
                prior_start_text = (control_at - timedelta(days=7)).astimezone(ZoneInfo("UTC")).isoformat(timespec="microseconds").replace("+00:00", "Z")
                candidates: dict[str, tuple[str, ...]] = {}
                episode_clause = "episode_id = 'base'" if episode_id == "base" else "episode_id IN ('base', ?)"
                for household_id_candidate in households:
                    prior_senders = {
                        row[0]
                        for row in operational.execute(
                            "SELECT DISTINCT customer_id FROM transactions "
                            f"WHERE {episode_clause} AND recipient_customer_id = ? AND available_at <= ? "
                            "AND event_at > ? AND event_at <= ?",
                            ((episode_id, household_id_candidate, control_at_text, prior_start_text, control_at_text)
                             if episode_id != "base" else
                             (household_id_candidate, control_at_text, prior_start_text, control_at_text)),
                        )
                    }
                    eligible = tuple(item for item in individuals if item not in prior_senders)
                    available_older = tuple(item for item in older_individuals if item in eligible)
                    if (
                        len(eligible) >= fan_in
                        and len(available_older) >= min(social.older_sender_minimum_count.value, fan_in)
                    ):
                        candidates[household_id_candidate] = eligible
                if not candidates:
                    raise ValueError("P1.4 requires a household control with enough unused senders")
                control_rng = keyed_rng(
                    seed, "p1.fraud.legit_business_spike.recipient", *scope, campaign_date.isoformat()
                )
                household_id = control_rng.choice(tuple(sorted(candidates)))
                eligible = candidates[household_id]
                sender_rng = keyed_rng(
                    seed, "p1.fraud.social_engineering.senders", *scope, campaign_date.isoformat()
                )
                older_count = min(social.older_sender_minimum_count.value, fan_in)
                selected_older = sender_rng.sample(
                    tuple(item for item in older_individuals if item in eligible), older_count
                )
                remaining_senders = tuple(item for item in eligible if item not in selected_older)
                selected_others = sender_rng.sample(remaining_senders, fan_in - older_count)
                senders = [*selected_older, *selected_others]
                mule_id = add_new_mule(
                    campaign_date,
                    group_index,
                    social.start_hour_local.value,
                    warmup_days=social.new_account_warmup_days.value,
                )
                cashout_candidates = tuple(item for item in individuals if item not in senders)
                cashout_id = keyed_rng(
                    seed, "p1.fraud.social_engineering.cashout", *scope, campaign_date.isoformat()
                ).choice(cashout_candidates or senders)
                incoming_ids: list[str] = []
                outgoing_ids: list[str] = []
                business_ids: list[str] = []
                business_amounts: list[int] = []
                amounts: list[int] = []
                fraud_events = 0
                business_events = 0
                for index, sender_id in enumerate(senders):
                    amount = keyed_rng(
                        seed, "p1.fraud.social_engineering.amount", *scope,
                        campaign_date.isoformat(), index,
                    ).randint(amount_min, amount_max)
                    amounts.append(amount)
                    incoming_at = starts + timedelta(days=index * social.incoming_spacing_days.value)
                    sender_device = device_id_for_transaction(
                        identity_seed=identity_seed, draw_seed=seed, episode_id=episode_id,
                        customer_id=sender_id, transaction_key=f"social-in-{campaign_date}-{index}",
                        change_probability=0.0, force_new=False,
                    )
                    txn_id, event_count = write_transaction(
                        namespace="social_engineering", group_index=group_index,
                        transaction_index=index, sender_id=sender_id, recipient_id=mule_id,
                        merchant_id=None, channel="wallet", amount=amount,
                        created_at=incoming_at, device_id=sender_device,
                    )
                    incoming_ids.append(txn_id)
                    fraud_events += event_count

                    control_amount = keyed_rng(
                        seed, "p1.fraud.legit_business_spike.amount", *scope,
                        campaign_date.isoformat(), index,
                    ).randint(
                        social.business_control_minimum_amount.value,
                        social.business_control_maximum_amount.value,
                    )
                    business_amounts.append(control_amount)
                    control_at = incoming_at + timedelta(hours=2)
                    control_device = device_id_for_transaction(
                        identity_seed=identity_seed, draw_seed=seed, episode_id=episode_id,
                        customer_id=sender_id, transaction_key=f"business-in-{campaign_date}-{index}",
                        change_probability=0.0, force_new=False,
                    )
                    control_id, event_count = write_transaction(
                        namespace="legit_business_spike", group_index=group_index,
                        transaction_index=index, sender_id=sender_id,
                        recipient_id=household_id, merchant_id=None, channel="wallet",
                        amount=control_amount, created_at=control_at, device_id=control_device,
                    )
                    business_ids.append(control_id)
                    business_events += event_count

                for index, amount in enumerate(amounts):
                    outgoing_at = starts + timedelta(
                        days=index * social.incoming_spacing_days.value,
                        minutes=social.cashout_delay_minutes.value,
                    )
                    mule_device = device_id_for_transaction(
                        identity_seed=identity_seed, draw_seed=seed, episode_id=episode_id,
                        customer_id=mule_id, transaction_key=f"social-out-{campaign_date}-{index}",
                        change_probability=0.0, force_new=False,
                    )
                    txn_id, event_count = write_transaction(
                        namespace="social_engineering", group_index=group_index,
                        transaction_index=fan_in + index, sender_id=mule_id,
                        recipient_id=cashout_id, merchant_id=None, channel="wallet",
                        amount=amount, created_at=outgoing_at, device_id=mule_device,
                    )
                    outgoing_ids.append(txn_id)
                    fraud_events += event_count

                group_id = f"p14-social-engineering-{group_index:04d}"
                groups.extend((
                    {
                        "mechanism": "social_engineering", "role": "fraud",
                        "group_id": group_id, "victim_customer_ids": list(senders),
                        "mule_customer_id": mule_id, "mule_role": "internal_mule",
                        "cashout_customer_id": cashout_id,
                        "incoming_transaction_ids": incoming_ids,
                        "outgoing_transaction_ids": outgoing_ids, "amounts": amounts,
                        "start_at": starts.isoformat(),
                        "end_at": (
                            starts + campaign_span + timedelta(minutes=social.cashout_delay_minutes.value)
                        ).isoformat(),
                    },
                    {
                        "mechanism": "legit_business_spike", "role": "benign_control",
                        "group_id": group_id, "sender_customer_ids": list(senders),
                        "recipient_customer_id": household_id,
                        "transaction_ids": business_ids, "fan_in_count": fan_in,
                        "amounts": business_amounts,
                        "start_at": (starts + timedelta(hours=2)).isoformat(),
                        "end_at": (starts + campaign_span + timedelta(hours=2)).isoformat(),
                    },
                ))
                summary["social_engineering_transaction_count"] += len(incoming_ids) + len(outgoing_ids)
                summary["business_control_transaction_count"] += len(business_ids)
                summary["lifecycle_event_count"] += fraud_events + business_events

        insert_private_record(
            private,
            "run_metadata",
            {
                "key": metadata_key,
                "value_json": {
                    "algorithm": "p1.4-fraud-mechanisms-v1",
                    "episode_id": episode_id,
                    "seed": seed,
                    "summary": summary,
                    "groups": groups,
                },
            },
        )
        private.execute("RELEASE SAVEPOINT p1_4_mechanisms_metadata")
        operational.execute("RELEASE SAVEPOINT p1_4_mechanisms")
    except Exception:
        private.execute("ROLLBACK TO SAVEPOINT p1_4_mechanisms_metadata")
        private.execute("RELEASE SAVEPOINT p1_4_mechanisms_metadata")
        operational.execute("ROLLBACK TO SAVEPOINT p1_4_mechanisms")
        operational.execute("RELEASE SAVEPOINT p1_4_mechanisms")
        raise
    return summary
