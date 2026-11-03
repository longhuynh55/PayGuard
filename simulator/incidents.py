"""P1.6 operational incident injectors and private truth."""

from __future__ import annotations

import json
import math
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Iterator, Literal, MutableMapping
from zoneinfo import ZoneInfo

from simulator.common import parse_timestamp
from simulator.config import SimulatorConfig
from simulator.fraud import generate_other_fraud_mechanisms, sample_card_testing_timing
from simulator.identifiers import transaction_id
from simulator.randomness import keyed_rng
from simulator.storage import insert_operational_record, insert_private_record, utc_timestamp
from simulator.transactions import card_token_for_customer, device_id_for_transaction
from simulator.world import WorldSummary


PIPELINE_START_OFFSET_DAYS = 1
INCIDENT_DEADLINE_DAYS = 7
PIPELINE_MODES = frozenset({"multiply_100", "null"})


@dataclass(frozen=True)
class IncidentSummary:
    incident_truth_id: str
    affected_transaction_ids: tuple[str, ...]
    effect_available_at: str
    calendar_event_id: str | None = None


@contextmanager
def _paired_savepoints(
    operational: sqlite3.Connection, private: sqlite3.Connection
) -> Iterator[None]:
    operational.execute("SAVEPOINT p1_6_incident_operational")
    private.execute("SAVEPOINT p1_6_incident_private")
    try:
        yield
    except Exception:
        operational.execute("ROLLBACK TO p1_6_incident_operational")
        private.execute("ROLLBACK TO p1_6_incident_private")
        operational.execute("RELEASE p1_6_incident_operational")
        private.execute("RELEASE p1_6_incident_private")
        raise
    else:
        operational.execute("RELEASE p1_6_incident_operational")
        private.execute("RELEASE p1_6_incident_private")


def _require_episode(
    operational: sqlite3.Connection, private: sqlite3.Connection, episode_id: str
) -> None:
    if not isinstance(episode_id, str) or not episode_id.strip() or episode_id == "base":
        raise ValueError("incident injection requires a registered non-base episode")
    operational_episode = operational.execute(
        "SELECT 1 FROM episodes WHERE episode_id = ? AND role <> 'base'", (episode_id,)
    ).fetchone()
    private_episode = private.execute(
        "SELECT 1 FROM private_episodes WHERE episode_id = ?", (episode_id,)
    ).fetchone()
    if operational_episode is None or private_episode is None:
        raise ValueError("incident episode must be registered in operational and private storage")


def _business_midnight(day: date, config: SimulatorConfig) -> datetime:
    timezone = ZoneInfo(config.timeline.business_timezone.value)
    return datetime.combine(day, time.min, tzinfo=timezone).astimezone(UTC)


def _check_truth_id(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("incident_truth_id must be a non-empty string")


def _write_truth(
    private: sqlite3.Connection,
    *,
    incident_truth_id: str,
    episode_id: str,
    cause: str,
    requires_intervention: bool,
    mechanism: str,
    rule_covered: bool,
    affected_entities: list[str],
    affected_segments: list[str],
    affected_features: list[str],
    affected_rows: list[sqlite3.Row],
    accepted_actions: list[str],
    pre_injection_amount_by_txn_id: dict[str, int] | None = None,
    parent_scenario: str | None = None,
) -> str:
    if parent_scenario is not None and (
        not isinstance(parent_scenario, str) or not parent_scenario.strip()
    ):
        raise ValueError("parent_scenario must be NULL or non-empty text")
    event_times = [row["event_at"] for row in affected_rows]
    available_times = [row["available_at"] for row in affected_rows]
    affected_transaction_ids = sorted({row["txn_id"] for row in affected_rows})
    if not affected_transaction_ids:
        raise ValueError("incident truth requires at least one affected transaction")
    effect_available_at = min(available_times)
    intervention_start = parse_timestamp(effect_available_at)
    insert_private_record(
        private,
        "incident_truth",
        {
            "incident_truth_id": incident_truth_id,
            "episode_id": episode_id,
            "cause": cause,
            "requires_intervention": int(requires_intervention),
            "mechanism": mechanism,
            "rule_covered": int(rule_covered),
            "affected_entities_json": sorted(set(affected_entities)),
            "affected_segments_json": sorted(set(affected_segments)),
            "affected_features_json": sorted(set(affected_features)),
            "effect_start_at": min(event_times),
            "effect_end_at": max(event_times),
            "effect_available_at": effect_available_at,
            "intervention_start_at": intervention_start,
            "deadline_at": intervention_start + timedelta(days=INCIDENT_DEADLINE_DAYS),
            "accepted_actions_json": accepted_actions,
            "parent_scenario": parent_scenario,
        },
    )
    effect_manifest = {
        "incident_truth_id": incident_truth_id,
        "episode_id": episode_id,
        "affected_transaction_ids": affected_transaction_ids,
    }
    if pre_injection_amount_by_txn_id is not None:
        effect_manifest["pre_injection_amount_by_txn_id"] = {
            txn_id: pre_injection_amount_by_txn_id[txn_id]
            for txn_id in affected_transaction_ids
        }
    insert_private_record(
        private,
        "run_metadata",
        {
            "key": f"p1.6.incident_effect.v1:{incident_truth_id}",
            "value_json": effect_manifest,
        },
    )
    return effect_available_at


def _write_truth_for_transactions(
    operational: sqlite3.Connection,
    private: sqlite3.Connection,
    *,
    incident_truth_id: str,
    episode_id: str,
    cause: str,
    requires_intervention: bool,
    mechanism: str,
    rule_covered: bool,
    affected_features: list[str],
    transaction_ids: list[str],
    accepted_actions: list[str],
    parent_scenario: str | None = None,
) -> str:
    if not transaction_ids or len(transaction_ids) != len(set(transaction_ids)):
        raise ValueError("incident truth requires a unique non-empty transaction set")
    placeholders = ", ".join("?" for _ in transaction_ids)
    rows = [
        dict(row)
        for row in operational.execute(
            "SELECT txn_id, episode_id, event_at, available_at, customer_id, "
            "merchant_id, recipient_customer_id, channel FROM transactions "
            f"WHERE txn_id IN ({placeholders}) ORDER BY event_at, txn_id",
            tuple(transaction_ids),
        )
    ]
    rows_by_id = {row["txn_id"]: row for row in rows}
    if set(rows_by_id) != set(transaction_ids):
        raise ValueError("incident truth references missing operational transactions")
    if any(row["episode_id"] != episode_id for row in rows):
        raise ValueError("incident truth transactions must belong to its episode")
    customer_ids = {
        value
        for row in rows
        for value in (row["customer_id"], row["recipient_customer_id"])
        if value is not None
    }
    merchant_ids = {
        row["merchant_id"] for row in rows if row["merchant_id"] is not None
    }
    segment_ids = sorted(customer_ids)
    if segment_ids:
        segment_placeholders = ", ".join("?" for _ in segment_ids)
        segments = [
            row[0]
            for row in operational.execute(
                "SELECT DISTINCT segment FROM customers "
                f"WHERE customer_id IN ({segment_placeholders}) ORDER BY segment",
                tuple(segment_ids),
            )
        ]
    else:
        segments = []
    return _write_truth(
        private,
        incident_truth_id=incident_truth_id,
        episode_id=episode_id,
        cause=cause,
        requires_intervention=requires_intervention,
        mechanism=mechanism,
        rule_covered=rule_covered,
        affected_entities=sorted(customer_ids | merchant_ids),
        affected_segments=segments,
        affected_features=affected_features,
        affected_rows=rows,
        accepted_actions=accepted_actions,
        parent_scenario=parent_scenario,
    )


def _p14_mechanism_groups(
    private: sqlite3.Connection, episode_id: str
) -> list[dict[str, Any]]:
    key = "p1.4.mechanisms.v1" if episode_id == "base" else f"p1.4.mechanisms.v1:{episode_id}"
    row = private.execute(
        "SELECT value_json FROM run_metadata WHERE key = ?", (key,)
    ).fetchone()
    if row is None:
        raise ValueError(f"P1.4 mechanism metadata is missing for {episode_id}")
    metadata = json.loads(row[0])
    groups = metadata.get("groups") if isinstance(metadata, dict) else None
    if not isinstance(metadata, dict) or metadata.get("episode_id") != episode_id or not isinstance(groups, list):
        raise ValueError(f"P1.4 mechanism metadata is malformed for {episode_id}")
    return [group for group in groups if isinstance(group, dict)]


def _p14_group_transaction_ids(group: dict[str, Any]) -> list[str]:
    transaction_ids = [
        txn_id
        for field in ("transaction_ids", "incoming_transaction_ids", "outgoing_transaction_ids")
        for txn_id in group.get(field, [])
    ]
    return sorted(set(transaction_ids))


def _load_private_account_profiles(private: sqlite3.Connection) -> dict[str, Any]:
    row = private.execute(
        "SELECT value_json FROM run_metadata WHERE key = 'p1.3.account_profiles.v1'"
    ).fetchone()
    if row is None:
        return {}
    try:
        metadata = json.loads(row[0])
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("private P1.3 account profiles are invalid JSON") from exc
    if (
        not isinstance(metadata, dict)
        or not isinstance(metadata.get("account_roles"), dict)
        or not isinstance(metadata.get("familiar_payees"), dict)
        or not isinstance(metadata.get("supplier_merchants"), dict)
        or type(metadata.get("internal_mule_outbound_window_hours")) is not int
    ):
        raise ValueError("private P1.3 account profiles are malformed")
    return metadata


def _load_base_world_summary(
    operational: sqlite3.Connection, private: sqlite3.Connection
) -> WorldSummary:
    metadata = _load_private_account_profiles(private)
    if not metadata:
        raise ValueError("P1.6 P1.4-backed injection requires private P1.3 account profiles")
    customer_ids = tuple(
        row[0]
        for row in operational.execute(
            "SELECT customer_id FROM customers WHERE episode_id = 'base' ORDER BY customer_id"
        )
    )
    merchant_ids = tuple(
        row[0]
        for row in operational.execute(
            "SELECT merchant_id FROM merchants WHERE episode_id = 'base' ORDER BY merchant_id"
        )
    )
    return WorldSummary(
        customer_ids=customer_ids,
        merchant_ids=merchant_ids,
        account_roles=dict(metadata["account_roles"]),
        familiar_payees={key: tuple(value) for key, value in metadata["familiar_payees"].items()},
        supplier_merchants={key: tuple(value) for key, value in metadata["supplier_merchants"].items()},
        internal_mule_outbound_window_hours=metadata["internal_mule_outbound_window_hours"],
    )


def _append_p16_fraud_group(
    private: sqlite3.Connection,
    *,
    episode_id: str,
    seed: int,
    incident_truth_id: str,
    mechanism: str,
    transaction_ids: list[str],
) -> None:
    key = f"p1.6.fraud_groups.v1:{episode_id}"
    row = private.execute(
        "SELECT value_json FROM run_metadata WHERE key = ?", (key,)
    ).fetchone()
    if row is None:
        metadata: dict[str, Any] = {
            "algorithm": "p1.6-fraud-groups-v1",
            "episode_id": episode_id,
            "seed": seed,
            "groups": [],
        }
        insert_private_record(private, "run_metadata", {"key": key, "value_json": metadata})
    else:
        metadata = json.loads(row[0])
        if (
            not isinstance(metadata, dict)
            or metadata.get("episode_id") != episode_id
            or not isinstance(metadata.get("groups"), list)
        ):
            raise ValueError(f"P1.6 fraud group metadata is malformed for {episode_id}")
    metadata["groups"].append(
        {
            "incident_truth_id": incident_truth_id,
            "mechanism": mechanism,
            "role": "fraud",
            "transaction_ids": sorted(set(transaction_ids)),
        }
    )
    private.execute(
        "UPDATE run_metadata SET value_json = ? WHERE key = ?",
        (json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")), key),
    )


def _write_business_calendar_context(
    operational: sqlite3.Connection,
    *,
    config: SimulatorConfig,
    episode_id: str,
    business_date: date,
    event_name: str,
    channel: str,
    expected_relative_change: float,
    seed: int,
    event_key: str,
) -> str:
    identity_rng = keyed_rng(seed, "p1.6.business_calendar.identity", episode_id, event_key)
    calendar_event_id = f"calendar-{identity_rng.getrandbits(128):032x}"
    event_at = _business_midnight(business_date, config)
    insert_operational_record(
        operational,
        "business_calendar",
        {
            "calendar_event_id": calendar_event_id,
            "episode_id": episode_id,
            "event_at": event_at,
            "available_at": event_at,
            "business_date": business_date.isoformat(),
            "event_name": event_name,
            "lag_days": 0,
            "expected_effect_json": {
                "txn_count": {
                    "aggregation": "sum",
                    "relative_change": float(expected_relative_change),
                }
            },
            "scope_json": {"channel": [channel]},
        },
    )
    return calendar_event_id


def _insert_background_adjustment(
    operational: sqlite3.Connection,
    config: SimulatorConfig,
    *,
    seed: int,
    episode_id: str,
    txn_id: str,
    random_key: str,
    amount: int,
    approved: bool,
    settled_at: datetime,
) -> None:
    if not approved:
        return
    draw = keyed_rng(seed, "p1.transactions.adjustment_type", episode_id, random_key).random()
    transactions = config.transactions
    if draw < transactions.refund_probability.value:
        event_type = "refunded"
    elif draw < transactions.refund_probability.value + transactions.reversal_probability.value:
        event_type = "reversed"
    else:
        return
    delay_seconds = keyed_rng(
        seed, "p1.transactions.adjustment_time", episode_id, random_key
    ).randint(
        transactions.adjustment_minimum_delay_minutes.value * 60,
        transactions.adjustment_maximum_delay_hours.value * 3600,
    )
    adjustment_at = settled_at + timedelta(seconds=delay_seconds)
    episode_end = _business_midnight(
        config.timeline.t0_business_date.value
        + timedelta(days=config.episode.duration_days.value),
        config,
    )
    if adjustment_at >= episode_end:
        return
    adjustment_amount = keyed_rng(
        seed, "p1.transactions.adjustment_amount", episode_id, random_key
    ).randint(1, amount)
    insert_operational_record(
        operational,
        "transaction_events",
        {
            "event_id": f"{txn_id}:{event_type}",
            "txn_id": txn_id,
            "episode_id": episode_id,
            "event_type": event_type,
            "event_at": adjustment_at,
            "available_at": adjustment_at,
            "amount": adjustment_amount,
            "response_code": None,
        },
    )


def _insert_generated_transaction(
    operational: sqlite3.Connection,
    config: SimulatorConfig,
    *,
    seed: int,
    episode_id: str,
    incident_truth_id: str,
    namespace: str,
    index: int,
    customer_id: str,
    merchant_id: str,
    channel: str,
    amount: int,
    created_at: datetime,
    approved: bool,
    transaction_random_keys: MutableMapping[str, str] | None,
) -> dict[str, Any]:
    if created_at.second == 0:
        created_at += timedelta(seconds=1)
    txn_id = transaction_id(
        seed, f"p1.6.{namespace}.transaction", episode_id, incident_truth_id, index
    )
    random_key = f"txn-{keyed_rng(seed, f'p1.6.{namespace}.label_key', episode_id, incident_truth_id, index).getrandbits(128):032x}"
    if transaction_random_keys is not None:
        transaction_random_keys[txn_id] = random_key
    insert_operational_record(
        operational,
        "transactions",
        {
            "txn_id": txn_id,
            "episode_id": episode_id,
            "customer_id": customer_id,
            "merchant_id": merchant_id,
            "recipient_customer_id": None,
            "device_id": device_id_for_transaction(
                identity_seed=config.seed.value,
                draw_seed=seed,
                episode_id=episode_id,
                customer_id=customer_id,
                transaction_key=random_key,
                change_probability=config.world.legitimate_device_change_probability.value,
            ),
            "card_token": card_token_for_customer(config.seed.value, customer_id)
            if channel == "card"
            else None,
            "created_at": created_at,
            "event_at": created_at,
            "available_at": created_at,
            "amount": amount,
            "channel": channel,
        },
    )
    authorized_at = created_at + timedelta(
        seconds=config.transactions.authorization_delay_seconds.value
    )
    authorization_type = "authorized" if approved else "declined"
    insert_operational_record(
        operational,
        "transaction_events",
        {
            "event_id": f"{txn_id}:{authorization_type}",
            "txn_id": txn_id,
            "episode_id": episode_id,
            "event_type": authorization_type,
            "event_at": authorized_at,
            "available_at": authorized_at,
            "response_code": config.transactions.authorization_response_code.value
            if approved
            else None,
        },
    )
    if approved:
        settled_at = authorized_at + timedelta(
            minutes=config.transactions.settlement_delay_minutes.value
        )
        insert_operational_record(
            operational,
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
        _insert_background_adjustment(
            operational,
            config,
            seed=seed,
            episode_id=episode_id,
            txn_id=txn_id,
            random_key=random_key,
            amount=amount,
            approved=True,
            settled_at=settled_at,
        )
    return dict(
        operational.execute(
            "SELECT txn_id, episode_id, event_at, available_at, customer_id, merchant_id, "
            "recipient_customer_id, channel, amount FROM transactions WHERE txn_id = ?",
            (txn_id,),
        ).fetchone()
    )


def _episode_business_customers_and_merchants(
    operational: sqlite3.Connection, episode_id: str, available_at: str
) -> tuple[list[str], list[str]]:
    customers = [
        row[0]
        for row in operational.execute(
            "SELECT customer_id FROM customers WHERE episode_id IN ('base', ?) "
            "AND available_at <= ? ORDER BY customer_id",
            (episode_id, available_at),
        )
    ]
    merchants = [
        row[0]
        for row in operational.execute(
            "SELECT merchant_id FROM merchants WHERE episode_id IN ('base', ?) "
            "AND available_at <= ? ORDER BY merchant_id",
            (episode_id, available_at),
        )
    ]
    if not customers or not merchants:
        raise ValueError("incident injection requires available customers and merchants")
    return customers, merchants


def inject_fraud_surge(
    operational: sqlite3.Connection,
    private: sqlite3.Connection,
    config: SimulatorConfig,
    *,
    episode_id: str,
    incident_truth_id: str,
    business_date: date,
    seed: int | None = None,
    transaction_random_keys: MutableMapping[str, str] | None = None,
    parent_scenario: str | None = None,
    calendar_event_name: str | None = None,
    calendar_channel: str | None = None,
    calendar_expected_relative_change: float | None = None,
    overlap_start_at: datetime | None = None,
) -> IncidentSummary:
    """Inject one new, known card-testing burst with an optional public calendar date."""
    _require_episode(operational, private, episode_id)
    _check_truth_id(incident_truth_id)
    _require_business_date(business_date)
    if business_date <= config.timeline.t0_business_date.value or business_date >= config.timeline.end_business_date_exclusive.value:
        raise ValueError("fraud surge must fall within the configured episode future")
    if parent_scenario is not None and (not isinstance(parent_scenario, str) or not parent_scenario.strip()):
        raise ValueError("parent_scenario must be NULL or non-empty text")
    if calendar_event_name is not None and (
        not isinstance(calendar_event_name, str) or not calendar_event_name.strip()
    ):
        raise ValueError("calendar_event_name must be non-empty when a calendar date is published")
    if calendar_event_name is not None:
        if calendar_channel not in config.transactions.channel_weights.value:
            raise ValueError("calendar_channel must be a configured transaction channel")
        if (
            isinstance(calendar_expected_relative_change, bool)
            or not isinstance(calendar_expected_relative_change, (int, float))
            or not math.isfinite(calendar_expected_relative_change)
            or calendar_expected_relative_change <= 0
        ):
            raise ValueError("calendar expected relative change must be finite and positive")

    run_seed = config.seed.value if seed is None else seed
    mechanism = config.fraud.card_testing
    if overlap_start_at is None:
        local_start = datetime.combine(
            business_date,
            time(mechanism.start_hour_local.value),
            tzinfo=ZoneInfo(config.timeline.business_timezone.value),
        )
        start_at, spacing_intervals, followup_delay_seconds = sample_card_testing_timing(
            config,
            seed=run_seed,
            namespace="fraud_surge",
            keys=(episode_id, incident_truth_id),
            base_start=local_start,
            attempts_per_card=mechanism.attempts_per_card.value,
        )
    else:
        if (
            not isinstance(overlap_start_at, datetime)
            or overlap_start_at.tzinfo is None
            or overlap_start_at.utcoffset() is None
        ):
            raise ValueError("overlap_start_at must be a timezone-aware datetime")
        start_at = overlap_start_at.astimezone(UTC)
        if start_at.astimezone(
            ZoneInfo(config.timeline.business_timezone.value)
        ).date() != business_date:
            raise ValueError("overlap_start_at must fall on the requested business date")
        _, spacing_intervals, followup_delay_seconds = sample_card_testing_timing(
            config,
            seed=run_seed,
            namespace="fraud_surge",
            keys=(episode_id, incident_truth_id),
            base_start=start_at,
            attempts_per_card=config.fraud.card_testing.attempts_per_card.value,
        )
    start_text = utc_timestamp(start_at)
    customers, merchants = _episode_business_customers_and_merchants(
        operational, episode_id, start_text
    )
    customer_id = keyed_rng(
        run_seed, "p1.6.fraud_surge.customer", episode_id, incident_truth_id
    ).choice(customers)
    merchant_id = keyed_rng(
        run_seed, "p1.6.fraud_surge.merchant", episode_id, incident_truth_id
    ).choice(merchants)
    attempt_count = mechanism.attempts_per_card.value
    attempt_offsets = [0]
    for spacing in spacing_intervals:
        attempt_offsets.append(attempt_offsets[-1] + spacing)
    affected_rows: list[dict[str, Any]] = []
    calendar_event_id: str | None = None
    with _paired_savepoints(operational, private):
        for index in range(attempt_count):
            amount = keyed_rng(
                run_seed,
                "p1.6.fraud_surge.attempt_amount",
                episode_id,
                incident_truth_id,
                index,
            ).randint(
                mechanism.minimum_attempt_amount.value,
                mechanism.maximum_attempt_amount.value,
            )
            approved = keyed_rng(
                run_seed,
                "p1.6.fraud_surge.attempt_approval",
                episode_id,
                incident_truth_id,
                index,
            ).random() < mechanism.approval_probability.value
            affected_rows.append(
                _insert_generated_transaction(
                    operational,
                    config,
                    seed=run_seed,
                    episode_id=episode_id,
                    incident_truth_id=incident_truth_id,
                    namespace="fraud_surge",
                    index=index,
                    customer_id=customer_id,
                    merchant_id=merchant_id,
                    channel="card",
                    amount=amount,
                    created_at=start_at + timedelta(seconds=attempt_offsets[index]),
                    approved=approved,
                    transaction_random_keys=transaction_random_keys,
                )
            )
        followup_index = attempt_count
        followup_at = start_at + timedelta(
            minutes=mechanism.window_minutes.value,
            seconds=followup_delay_seconds,
        )
        followup_amount = keyed_rng(
            run_seed,
            "p1.6.fraud_surge.followup_amount",
            episode_id,
            incident_truth_id,
        ).randint(
            mechanism.minimum_followup_amount.value,
            mechanism.maximum_followup_amount.value,
        )
        followup_approved = keyed_rng(
            run_seed,
            "p1.6.fraud_surge.followup_approval",
            episode_id,
            incident_truth_id,
        ).random() < mechanism.followup_approval_probability.value
        affected_rows.append(
            _insert_generated_transaction(
                operational,
                config,
                seed=run_seed,
                episode_id=episode_id,
                incident_truth_id=incident_truth_id,
                namespace="fraud_surge",
                index=followup_index,
                customer_id=customer_id,
                merchant_id=merchant_id,
                channel="card",
                amount=followup_amount,
                created_at=followup_at,
                approved=followup_approved,
                transaction_random_keys=transaction_random_keys,
            )
        )
        transaction_ids = [row["txn_id"] for row in affected_rows]
        _append_p16_fraud_group(
            private,
            episode_id=episode_id,
            seed=run_seed,
            incident_truth_id=incident_truth_id,
            mechanism="fraud_surge",
            transaction_ids=transaction_ids,
        )
        if calendar_event_name is not None:
            calendar_event_id = _write_business_calendar_context(
                operational,
                config=config,
                episode_id=episode_id,
                business_date=business_date,
                event_name=calendar_event_name,
                channel=calendar_channel,
                expected_relative_change=calendar_expected_relative_change,
                seed=run_seed,
                event_key=incident_truth_id,
            )
        effect_available_at = _write_truth(
            private,
            incident_truth_id=incident_truth_id,
            episode_id=episode_id,
            cause="fraud_surge",
            requires_intervention=True,
            mechanism="known_card_testing_surge",
            rule_covered=True,
            affected_entities=[customer_id, merchant_id],
            affected_segments=[
                row[0]
                for row in operational.execute(
                    "SELECT DISTINCT segment FROM customers WHERE customer_id = ?", (customer_id,)
                )
            ],
            affected_features=["amount", "txn_count"],
            affected_rows=affected_rows,
            accepted_actions=["fraud_ops", "human_review"],
            parent_scenario=parent_scenario,
        )
    return IncidentSummary(
        incident_truth_id=incident_truth_id,
        affected_transaction_ids=tuple(row["txn_id"] for row in affected_rows),
        effect_available_at=effect_available_at,
        calendar_event_id=calendar_event_id,
    )


def inject_new_fraud(
    operational: sqlite3.Connection,
    private: sqlite3.Connection,
    config: SimulatorConfig,
    *,
    episode_id: str,
    incident_truth_id: str,
    seed: int,
    transaction_random_keys: MutableMapping[str, str],
    parent_scenario: str | None = None,
) -> IncidentSummary:
    """Generate a P1.4 social-engineering chain, then record private truth."""
    _require_episode(operational, private, episode_id)
    _check_truth_id(incident_truth_id)
    if any(
        item.get("mechanism") == "social_engineering"
        for item in _p14_mechanism_groups(private, episode_id)
    ):
        raise ValueError("new_fraud social-engineering chain has already been generated")
    run_seed = seed
    world_summary = _load_base_world_summary(operational, private)
    with _paired_savepoints(operational, private):
        generate_other_fraud_mechanisms(
            operational,
            private,
            config,
            world_summary,
            seed=run_seed,
            episode_id=episode_id,
            duration_days=config.episode.duration_days.value,
            mechanisms={"social_engineering"},
            transaction_random_keys=transaction_random_keys,
        )
        group = next(
            (
                item
                for item in _p14_mechanism_groups(private, episode_id)
                if item.get("mechanism") == "social_engineering" and item.get("role") == "fraud"
            ),
            None,
        )
        if group is None:
            raise ValueError("P1.4 writer did not generate a social-engineering group")
        transaction_ids = _p14_group_transaction_ids(group)
        effect_available_at = _write_truth_for_transactions(
            operational,
            private,
            incident_truth_id=incident_truth_id,
            episode_id=episode_id,
            cause="new_fraud",
            requires_intervention=True,
            mechanism="social_engineering",
            rule_covered=False,
            affected_features=[
                "recipient_fan_in_7d",
                "account_outflow_ratio_24h",
                "new_or_dormant_recipient",
            ],
            transaction_ids=transaction_ids,
            accepted_actions=["human_review", "fraud_ops"],
            parent_scenario=parent_scenario,
        )
    return IncidentSummary(
        incident_truth_id=incident_truth_id,
        affected_transaction_ids=tuple(transaction_ids),
        effect_available_at=effect_available_at,
    )


def inject_silent_concept_drift(
    operational: sqlite3.Connection,
    private: sqlite3.Connection,
    config: SimulatorConfig,
    *,
    episode_id: str,
    incident_truth_id: str,
    business_date: date,
    transaction_count: int,
    seed: int | None = None,
    transaction_random_keys: MutableMapping[str, str] | None = None,
) -> IncidentSummary:
    """Add fraud transactions sampled from ordinary payment fields and ranges."""
    _require_episode(operational, private, episode_id)
    _check_truth_id(incident_truth_id)
    _require_business_date(business_date)
    if business_date <= config.timeline.t0_business_date.value or business_date >= config.timeline.end_business_date_exclusive.value:
        raise ValueError("silent concept drift must fall within the configured episode future")
    if (
        isinstance(transaction_count, bool)
        or not isinstance(transaction_count, int)
        or transaction_count <= 0
    ):
        raise ValueError("transaction_count must be a positive integer")
    run_seed = config.seed.value if seed is None else seed
    account_profiles = _load_private_account_profiles(private)
    account_roles = account_profiles.get("account_roles", {})
    supplier_merchants = account_profiles.get("supplier_merchants", {})
    business_midnight = _business_midnight(business_date, config)
    available_text = utc_timestamp(business_midnight)
    customers, merchants = _episode_business_customers_and_merchants(
        operational, episode_id, available_text
    )
    hourly_weights = config.transactions.hourly_transaction_weights.value
    channel_weights = config.transactions.channel_weights.value
    channels = tuple(sorted(channel_weights))
    channel_weight_values = [channel_weights[channel] for channel in channels]
    rows: list[dict[str, Any]] = []
    with _paired_savepoints(operational, private):
        for index in range(transaction_count):
            choice_rng = keyed_rng(
                run_seed,
                "p1.6.silent_concept_drift.normal_fields",
                episode_id,
                incident_truth_id,
                index,
            )
            hour = choice_rng.choices(range(24), weights=hourly_weights, k=1)[0]
            channel = choice_rng.choices(
                channels, weights=channel_weight_values, k=1
            )[0]
            created_at = _business_midnight(business_date, config) + timedelta(hours=hour)
            time_rng = keyed_rng(
                run_seed,
                "p1.6.silent_concept_drift.transaction_time",
                episode_id,
                incident_truth_id,
                index,
            )
            created_at += timedelta(
                minutes=time_rng.randint(0, config.fraud.timing_jitter.maximum_minute_offset.value),
                seconds=time_rng.randint(1, config.fraud.timing_jitter.maximum_second_offset.value),
            )
            customer_id = choice_rng.choice(customers)
            available_merchants = merchants
            if account_roles.get(customer_id) == "household_business":
                suppliers = set(supplier_merchants.get(customer_id, []))
                available_merchants = [item for item in merchants if item in suppliers]
                if not available_merchants:
                    raise ValueError(
                        f"silent-drift household {customer_id} has no available supplier merchant"
                    )
            rows.append(
                _insert_generated_transaction(
                    operational,
                    config,
                    seed=run_seed,
                    episode_id=episode_id,
                    incident_truth_id=incident_truth_id,
                    namespace="silent_concept_drift",
                    index=index,
                    customer_id=customer_id,
                    merchant_id=choice_rng.choice(available_merchants),
                    channel=channel,
                    amount=choice_rng.randint(
                        config.transactions.minimum_amount.value,
                        config.transactions.maximum_amount.value,
                    ),
                    created_at=created_at,
                    approved=True,
                    transaction_random_keys=transaction_random_keys,
                )
            )
        transaction_ids = [row["txn_id"] for row in rows]
        _append_p16_fraud_group(
            private,
            episode_id=episode_id,
            seed=run_seed,
            incident_truth_id=incident_truth_id,
            mechanism="silent_concept_drift",
            transaction_ids=transaction_ids,
        )
        affected_customer_ids = sorted({row["customer_id"] for row in rows})
        affected_merchant_ids = sorted({row["merchant_id"] for row in rows})
        effect_available_at = _write_truth(
            private,
            incident_truth_id=incident_truth_id,
            episode_id=episode_id,
            cause="new_fraud",
            requires_intervention=True,
            mechanism="silent_concept_drift",
            rule_covered=False,
            affected_entities=sorted(set(affected_customer_ids) | set(affected_merchant_ids)),
            affected_segments=[
                row[0]
                for row in operational.execute(
                    "SELECT DISTINCT segment FROM customers WHERE customer_id IN ("
                    + ",".join("?" for _ in affected_customer_ids)
                    + ") ORDER BY segment",
                    tuple(affected_customer_ids),
                )
            ],
            affected_features=["txn_count", "amount"],
            affected_rows=rows,
            accepted_actions=["human_review", "fraud_ops"],
        )
    return IncidentSummary(
        incident_truth_id=incident_truth_id,
        affected_transaction_ids=tuple(transaction_ids),
        effect_available_at=effect_available_at,
    )


def inject_legit_business_spike(
    operational: sqlite3.Connection,
    private: sqlite3.Connection,
    config: SimulatorConfig,
    *,
    episode_id: str,
    incident_truth_id: str,
    seed: int,
    transaction_random_keys: MutableMapping[str, str],
) -> IncidentSummary:
    """Generate a P1.4 household-business spike, then record benign private truth."""
    _require_episode(operational, private, episode_id)
    _check_truth_id(incident_truth_id)
    if any(
        item.get("mechanism") == "legit_business_spike"
        for item in _p14_mechanism_groups(private, episode_id)
    ):
        raise ValueError("legit_business_spike chain has already been generated")
    run_seed = seed
    world_summary = _load_base_world_summary(operational, private)
    with _paired_savepoints(operational, private):
        generate_other_fraud_mechanisms(
            operational,
            private,
            config,
            world_summary,
            seed=run_seed,
            episode_id=episode_id,
            duration_days=config.episode.duration_days.value,
            mechanisms={"legit_business_spike"},
            transaction_random_keys=transaction_random_keys,
        )
        group = next(
            (
                item
                for item in _p14_mechanism_groups(private, episode_id)
                if item.get("mechanism") == "legit_business_spike"
                and item.get("role") == "benign_control"
            ),
            None,
        )
        if group is None:
            raise ValueError("P1.4 writer did not generate a business-control group")
        transaction_ids = _p14_group_transaction_ids(group)
        effect_available_at = _write_truth_for_transactions(
            operational,
            private,
            incident_truth_id=incident_truth_id,
            episode_id=episode_id,
            cause="legit_business_change",
            requires_intervention=False,
            mechanism="legit_business_spike",
            rule_covered=False,
            affected_features=["txn_count"],
            transaction_ids=transaction_ids,
            accepted_actions=[],
        )
    return IncidentSummary(
        incident_truth_id=incident_truth_id,
        affected_transaction_ids=tuple(transaction_ids),
        effect_available_at=effect_available_at,
    )


def inject_sparse_new_merchant(
    operational: sqlite3.Connection,
    private: sqlite3.Connection,
    config: SimulatorConfig,
    *,
    episode_id: str,
    incident_truth_id: str,
    truth_cause: Literal["new_fraud", "no_material_change"],
    business_date: date,
    transaction_count: int = 3,
    seed: int | None = None,
    transaction_random_keys: MutableMapping[str, str] | None = None,
) -> IncidentSummary:
    """Create a new merchant with few transactions and caller-selected private truth."""
    _require_episode(operational, private, episode_id)
    _check_truth_id(incident_truth_id)
    _require_business_date(business_date)
    if business_date <= config.timeline.t0_business_date.value or business_date >= config.timeline.end_business_date_exclusive.value:
        raise ValueError("sparse new-merchant scenario must fall within the configured episode future")
    if not isinstance(truth_cause, str) or truth_cause not in {
        "new_fraud",
        "no_material_change",
    }:
        raise ValueError("truth_cause must be new_fraud or no_material_change")
    if (
        isinstance(transaction_count, bool)
        or not isinstance(transaction_count, int)
        or not 2 <= transaction_count <= 5
    ):
        raise ValueError("transaction_count must be between two and five")

    run_seed = config.seed.value if seed is None else seed
    business_midnight = _business_midnight(business_date, config)
    customers, source_merchant_ids = _episode_business_customers_and_merchants(
        operational, episode_id, utc_timestamp(business_midnight)
    )
    if not customers or not source_merchant_ids:
        raise ValueError("sparse new-merchant scenario requires available customers and merchants")
    account_roles = _load_private_account_profiles(private).get("account_roles", {})
    individual_customers = [
        customer_id for customer_id in customers if account_roles.get(customer_id) == "individual"
    ]
    customer_candidates = individual_customers or customers

    existing_merchant_ids = {
        row[0] for row in operational.execute("SELECT merchant_id FROM merchants")
    }
    numeric_merchant_ids = [
        int(value[len("merchant-") :])
        for value in existing_merchant_ids
        if value.startswith("merchant-")
        and value[len("merchant-") :].isdigit()
        and len(value[len("merchant-") :]) == 6
    ]
    merchant_start = max(numeric_merchant_ids, default=0) + 1
    merchant_id = next(
        (
            f"merchant-{(merchant_start + offset) % 1_000_000:06d}"
            for offset in range(1_000_000)
            if f"merchant-{(merchant_start + offset) % 1_000_000:06d}"
            not in existing_merchant_ids
        ),
        None,
    )
    if merchant_id is None:
        raise ValueError("unable to allocate a unique merchant ID")
    source_ids = tuple(sorted(source_merchant_ids))
    source_placeholders = ", ".join("?" for _ in source_ids)
    source_merchants = [
        dict(row)
        for row in operational.execute(
            "SELECT mcc, province FROM merchants "
            f"WHERE merchant_id IN ({source_placeholders}) ORDER BY merchant_id",
            source_ids,
        )
    ]
    profile_rng = keyed_rng(
        run_seed, "p1.6.sparse_new_merchant.profile", episode_id, incident_truth_id
    )
    merchant_profile = profile_rng.choice(source_merchants)
    merchant_time_rng = keyed_rng(
        run_seed, "p1.6.sparse_new_merchant.creation_time", episode_id, incident_truth_id
    )
    merchant_available_at = business_midnight + timedelta(
        minutes=merchant_time_rng.randint(
            0, config.fraud.timing_jitter.maximum_minute_offset.value
        ),
        seconds=merchant_time_rng.randint(
            1, config.fraud.timing_jitter.maximum_second_offset.value
        ),
    )
    hourly_weights = config.transactions.hourly_transaction_weights.value
    channel_weights = config.transactions.channel_weights.value
    channels = tuple(sorted(channel_weights))
    channel_weight_values = [channel_weights[channel] for channel in channels]
    rows: list[dict[str, Any]] = []

    with _paired_savepoints(operational, private):
        insert_operational_record(
            operational,
            "merchants",
            {
                "merchant_id": merchant_id,
                "episode_id": episode_id,
                "event_at": merchant_available_at,
                "available_at": merchant_available_at,
                "mcc": merchant_profile["mcc"],
                "province": merchant_profile["province"],
            },
        )
        for index in range(transaction_count):
            field_rng = keyed_rng(
                run_seed,
                "p1.6.sparse_new_merchant.fields",
                episode_id,
                incident_truth_id,
                index,
            )
            time_rng = keyed_rng(
                run_seed,
                "p1.6.sparse_new_merchant.time",
                episode_id,
                incident_truth_id,
                index,
            )
            hour = time_rng.choices(range(24), weights=hourly_weights, k=1)[0]
            created_at = business_midnight + timedelta(
                hours=hour,
                minutes=time_rng.randint(0, config.fraud.timing_jitter.maximum_minute_offset.value),
                seconds=time_rng.randint(1, config.fraud.timing_jitter.maximum_second_offset.value),
            )
            if created_at.astimezone(
                ZoneInfo(config.timeline.business_timezone.value)
            ).date() != business_date:
                created_at = business_midnight + timedelta(
                    hours=23,
                    minutes=59,
                    seconds=config.fraud.timing_jitter.maximum_second_offset.value,
                )
            if created_at <= merchant_available_at:
                created_at = merchant_available_at + timedelta(minutes=index + 1)
            rows.append(
                _insert_generated_transaction(
                    operational,
                    config,
                    seed=run_seed,
                    episode_id=episode_id,
                    incident_truth_id=incident_truth_id,
                    namespace="sparse_new_merchant",
                    index=index,
                    customer_id=field_rng.choice(customer_candidates),
                    merchant_id=merchant_id,
                    channel=field_rng.choices(
                        channels, weights=channel_weight_values, k=1
                    )[0],
                    amount=field_rng.randint(
                        config.transactions.minimum_amount.value,
                        config.transactions.maximum_amount.value,
                    ),
                    created_at=created_at,
                    approved=True,
                    transaction_random_keys=transaction_random_keys,
                )
            )
        transaction_ids = [row["txn_id"] for row in rows]
        if truth_cause == "new_fraud":
            _append_p16_fraud_group(
                private,
                episode_id=episode_id,
                seed=run_seed,
                incident_truth_id=incident_truth_id,
                mechanism="sparse_new_merchant",
                transaction_ids=transaction_ids,
            )
        effect_available_at = _write_truth_for_transactions(
            operational,
            private,
            incident_truth_id=incident_truth_id,
            episode_id=episode_id,
            cause=truth_cause,
            requires_intervention=truth_cause == "new_fraud",
            mechanism="new_merchant_sparse_history",
            rule_covered=False,
            affected_features=["txn_count"],
            transaction_ids=transaction_ids,
            accepted_actions=(
                ["human_review", "fraud_ops"]
                if truth_cause == "new_fraud"
                else []
            ),
        )
    return IncidentSummary(
        incident_truth_id=incident_truth_id,
        affected_transaction_ids=tuple(transaction_ids),
        effect_available_at=effect_available_at,
    )


def inject_seasonal_unlisted(
    operational: sqlite3.Connection,
    private: sqlite3.Connection,
    config: SimulatorConfig,
    *,
    episode_id: str,
    incident_truth_id: str,
    business_date: date,
    event_name: str,
    additional_transaction_count: int,
    expected_relative_change: float,
    seed: int | None = None,
    customer_ids: tuple[str, ...] | None = None,
    transaction_random_keys: MutableMapping[str, str] | None = None,
) -> IncidentSummary:
    """Inject the same seasonal traffic without publishing a calendar event."""
    return inject_seasonal(
        operational,
        private,
        config,
        episode_id=episode_id,
        incident_truth_id=incident_truth_id,
        business_date=business_date,
        event_name=event_name,
        additional_transaction_count=additional_transaction_count,
        expected_relative_change=expected_relative_change,
        seed=seed,
        customer_ids=customer_ids,
        transaction_random_keys=transaction_random_keys,
        publish_calendar=False,
        rule_covered=False,
    )


def inject_fraud_surge_on_calendar_date(
    operational: sqlite3.Connection,
    private: sqlite3.Connection,
    config: SimulatorConfig,
    *,
    episode_id: str,
    incident_truth_id: str,
    business_date: date,
    event_name: str = "Synthetic promotion event",
    channel: str | None = None,
    expected_relative_change: float = 5.0,
    seed: int | None = None,
    transaction_random_keys: MutableMapping[str, str] | None = None,
) -> IncidentSummary:
    """Publish a neutral calendar context and inject one fraud truth on that date."""
    return inject_fraud_surge(
        operational,
        private,
        config,
        episode_id=episode_id,
        incident_truth_id=incident_truth_id,
        business_date=business_date,
        seed=seed,
        transaction_random_keys=transaction_random_keys,
        calendar_event_name=event_name,
        calendar_channel=channel or config.transactions.channel.value,
        calendar_expected_relative_change=expected_relative_change,
    )


def inject_two_cause_fraud_seasonal(
    operational: sqlite3.Connection,
    private: sqlite3.Connection,
    config: SimulatorConfig,
    *,
    episode_id: str,
    fraud_truth_id: str,
    seasonal_truth_id: str,
    parent_scenario: str,
    business_date: date,
    event_name: str,
    seasonal_additional_transaction_count: int,
    seasonal_expected_relative_change: float,
    seed: int | None = None,
    customer_ids: tuple[str, ...] | None = None,
    transaction_random_keys: MutableMapping[str, str] | None = None,
) -> tuple[IncidentSummary, IncidentSummary]:
    """Inject overlapping fraud and seasonal components; publish calendar for seasonality only."""
    if not isinstance(parent_scenario, str) or not parent_scenario.strip():
        raise ValueError("two-cause injection requires a non-empty parent_scenario")
    operational.execute("SAVEPOINT p1_6_two_cause_operational")
    private.execute("SAVEPOINT p1_6_two_cause_private")
    try:
        seasonal = inject_seasonal(
            operational,
            private,
            config,
            episode_id=episode_id,
            incident_truth_id=seasonal_truth_id,
            business_date=business_date,
            event_name=event_name,
            additional_transaction_count=seasonal_additional_transaction_count,
            expected_relative_change=seasonal_expected_relative_change,
            seed=seed,
            customer_ids=customer_ids,
            transaction_random_keys=transaction_random_keys,
            publish_calendar=True,
            rule_covered=True,
            parent_scenario=parent_scenario,
        )
        seasonal_placeholders = ", ".join("?" for _ in seasonal.affected_transaction_ids)
        seasonal_event_times = [
            datetime.fromisoformat(row[0].replace("Z", "+00:00")).astimezone(UTC)
            for row in operational.execute(
                "SELECT event_at FROM transactions "
                f"WHERE txn_id IN ({seasonal_placeholders})",
                seasonal.affected_transaction_ids,
            )
        ]
        seasonal_start = min(seasonal_event_times)
        seasonal_end = max(seasonal_event_times)
        overlap_seed = config.seed.value if seed is None else seed
        local_start = datetime.combine(
            business_date,
            time(config.fraud.card_testing.start_hour_local.value),
            tzinfo=ZoneInfo(config.timeline.business_timezone.value),
        )
        sampled_start, _, sampled_followup_delay = sample_card_testing_timing(
            config,
            seed=overlap_seed,
            namespace="fraud_surge",
            keys=(episode_id, fraud_truth_id),
            base_start=local_start,
            attempts_per_card=config.fraud.card_testing.attempts_per_card.value,
        )
        fraud_end = sampled_start + timedelta(
            minutes=config.fraud.card_testing.window_minutes.value,
            seconds=sampled_followup_delay,
        )
        if sampled_start > seasonal_end or seasonal_start > fraud_end:
            overlap_seconds = int((seasonal_end - seasonal_start).total_seconds())
            fallback_rng = keyed_rng(
                overlap_seed,
                "p1.6.fraud_seasonal.overlap_fallback",
                episode_id,
                parent_scenario,
            )
            sampled_start = seasonal_start + timedelta(
                seconds=fallback_rng.randint(0, max(0, overlap_seconds))
            )
        overlap_start_at = sampled_start
        fraud = inject_fraud_surge(
            operational,
            private,
            config,
            episode_id=episode_id,
            incident_truth_id=fraud_truth_id,
            business_date=business_date,
            seed=seed,
            transaction_random_keys=transaction_random_keys,
            parent_scenario=parent_scenario,
            overlap_start_at=overlap_start_at,
        )
        fraud_truth = private.execute(
            "SELECT effect_start_at, effect_end_at FROM incident_truth "
            "WHERE incident_truth_id = ?",
            (fraud_truth_id,),
        ).fetchone()
        seasonal_truth = private.execute(
            "SELECT effect_start_at, effect_end_at FROM incident_truth "
            "WHERE incident_truth_id = ?",
            (seasonal_truth_id,),
        ).fetchone()
        if fraud_truth is None or seasonal_truth is None:
            raise ValueError("two-cause injection did not write both component truths")
        fraud_start, fraud_end = map(parse_timestamp, fraud_truth)
        seasonal_start, seasonal_end = map(parse_timestamp, seasonal_truth)
        if fraud_start > seasonal_end or seasonal_start > fraud_end:
            raise ValueError("two-cause fraud and seasonal effect windows must overlap")
        private.execute("RELEASE SAVEPOINT p1_6_two_cause_private")
        operational.execute("RELEASE SAVEPOINT p1_6_two_cause_operational")
        return fraud, seasonal
    except Exception:
        private.execute("ROLLBACK TO SAVEPOINT p1_6_two_cause_private")
        private.execute("RELEASE SAVEPOINT p1_6_two_cause_private")
        operational.execute("ROLLBACK TO SAVEPOINT p1_6_two_cause_operational")
        operational.execute("RELEASE SAVEPOINT p1_6_two_cause_operational")
        raise


def inject_data_pipeline(
    operational: sqlite3.Connection,
    private: sqlite3.Connection,
    config: SimulatorConfig,
    *,
    episode_id: str,
    incident_truth_id: str,
    mode: Literal["multiply_100", "null"],
) -> IncidentSummary:
    """Apply one contracted amount pipeline failure to post-T0 episode rows."""
    _require_episode(operational, private, episode_id)
    _check_truth_id(incident_truth_id)
    if mode not in PIPELINE_MODES:
        raise ValueError("mode must be 'multiply_100' or 'null'")

    first_affected_business_day = (
        config.timeline.t0_business_date.value + timedelta(days=PIPELINE_START_OFFSET_DAYS)
    )
    start = utc_timestamp(_business_midnight(first_affected_business_day, config))
    candidates = operational.execute(
        """
        SELECT txn_id, episode_id, amount, event_at, available_at, customer_id, merchant_id,
               recipient_customer_id, channel
        FROM transactions
        WHERE episode_id = ? AND event_at >= ? AND available_at >= ?
        ORDER BY event_at, txn_id
        """,
        (episode_id, start, start),
    ).fetchall()

    changed: list[sqlite3.Row] = []
    with _paired_savepoints(operational, private):
        for row in candidates:
            amount = row["amount"]
            if amount is None:
                continue
            if mode == "multiply_100":
                if amount == 0:
                    continue
                changed_amount = amount * 100
            else:
                changed_amount = None
            if changed_amount == amount:
                continue
            operational.execute(
                "UPDATE transactions SET amount = ? WHERE txn_id = ? AND episode_id = ?",
                (changed_amount, row["txn_id"], episode_id),
            )
            changed.append(row)

        if not changed:
            raise ValueError("data_pipeline requires at least one actually affected episode transaction")

        customer_ids = {
            row["customer_id"]
            for row in changed
        } | {
            row["recipient_customer_id"]
            for row in changed
            if row["recipient_customer_id"] is not None
        }
        merchant_ids = {
            row["merchant_id"] for row in changed if row["merchant_id"] is not None
        }
        segments = [
            row[0]
            for row in operational.execute(
                f"SELECT DISTINCT segment FROM customers WHERE customer_id IN ({','.join('?' for _ in customer_ids)})",
                tuple(sorted(customer_ids)),
            ).fetchall()
        ]
        effect_available_at = _write_truth(
            private,
            incident_truth_id=incident_truth_id,
            episode_id=episode_id,
            cause="data_pipeline",
            requires_intervention=True,
            mechanism="amount_multiplier_100" if mode == "multiply_100" else "amount_null_burst",
            rule_covered=True,
            affected_entities=sorted(customer_ids | merchant_ids),
            affected_segments=segments,
            affected_features=["amount"],
            affected_rows=changed,
            accepted_actions=["data_ticket", "human_review"],
            pre_injection_amount_by_txn_id={
                row["txn_id"]: row["amount"] for row in changed
            },
        )

    return IncidentSummary(
        incident_truth_id=incident_truth_id,
        affected_transaction_ids=tuple(row["txn_id"] for row in changed),
        effect_available_at=effect_available_at,
    )


def _require_business_date(value: date) -> None:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise ValueError("business_date must be a datetime.date")


def _validate_seasonal_parameters(
    *, business_date: date, event_name: str, additional_transaction_count: int,
    expected_relative_change: float,
) -> None:
    _require_business_date(business_date)
    if not isinstance(event_name, str) or not event_name.strip():
        raise ValueError("event_name must be a non-empty public business-calendar description")
    if (
        isinstance(additional_transaction_count, bool)
        or not isinstance(additional_transaction_count, int)
        or additional_transaction_count <= 0
    ):
        raise ValueError("additional_transaction_count must be a positive integer")
    if (
        isinstance(expected_relative_change, bool)
        or not isinstance(expected_relative_change, (int, float))
        or not math.isfinite(expected_relative_change)
        or expected_relative_change <= 0
    ):
        raise ValueError("expected_relative_change must be a finite positive number")


def inject_seasonal(
    operational: sqlite3.Connection,
    private: sqlite3.Connection,
    config: SimulatorConfig,
    *,
    episode_id: str,
    incident_truth_id: str,
    business_date: date,
    event_name: str,
    additional_transaction_count: int,
    expected_relative_change: float,
    seed: int | None = None,
    customer_ids: tuple[str, ...] | None = None,
    transaction_random_keys: MutableMapping[str, str] | None = None,
    publish_calendar: bool = True,
    rule_covered: bool = True,
    parent_scenario: str | None = None,
) -> IncidentSummary:
    """Insert valid one-day business traffic and publish its expected metric."""
    _require_episode(operational, private, episode_id)
    _check_truth_id(incident_truth_id)
    _validate_seasonal_parameters(
        business_date=business_date,
        event_name=event_name,
        additional_transaction_count=additional_transaction_count,
        expected_relative_change=expected_relative_change,
    )
    if not isinstance(publish_calendar, bool) or not isinstance(rule_covered, bool):
        raise ValueError("publish_calendar and rule_covered must be booleans")
    if parent_scenario is not None and (not isinstance(parent_scenario, str) or not parent_scenario.strip()):
        raise ValueError("parent_scenario must be NULL or non-empty text")
    if business_date <= config.timeline.t0_business_date.value:
        raise ValueError("seasonal event must occur after T0")
    if business_date >= config.timeline.end_business_date_exclusive.value:
        raise ValueError("seasonal event must fall within the configured timeline")

    run_seed = config.seed.value if seed is None else seed
    account_profiles = _load_private_account_profiles(private)
    account_roles = account_profiles.get("account_roles", {})
    supplier_merchants = account_profiles.get("supplier_merchants", {})
    traffic_rng = keyed_rng(
        run_seed, "p1.incidents.seasonal.traffic", episode_id, incident_truth_id,
        business_date.isoformat(),
    )
    identity_rng = keyed_rng(
        run_seed, "p1.incidents.seasonal.identities", episode_id, incident_truth_id,
        business_date.isoformat(),
    )
    event_at = _business_midnight(business_date, config)
    channels = [config.transactions.channel.value]
    if customer_ids is None:
        customers = operational.execute(
            """
            SELECT customer_id FROM customers
            WHERE episode_id IN ('base', ?) AND available_at <= ?
            ORDER BY customer_id
            """,
            (episode_id, utc_timestamp(event_at)),
        ).fetchall()
    else:
        allowed_customer_ids = tuple(sorted(set(customer_ids)))
        if not allowed_customer_ids:
            raise ValueError("seasonal event requires at least one eligible base customer")
        placeholders = ", ".join("?" for _ in allowed_customer_ids)
        customers = operational.execute(
            "SELECT customer_id FROM customers "
            f"WHERE episode_id IN ('base', ?) AND available_at <= ? "
            f"AND customer_id IN ({placeholders}) ORDER BY customer_id",
            (episode_id, utc_timestamp(event_at), *allowed_customer_ids),
        ).fetchall()
    merchants = operational.execute(
        """
        SELECT merchant_id FROM merchants
        WHERE episode_id IN ('base', ?) AND available_at <= ?
        ORDER BY merchant_id
        """,
        (episode_id, utc_timestamp(event_at)),
    ).fetchall()
    if not customers or not merchants:
        raise ValueError("seasonal event requires available episode or base customers and merchants")
    merchant_ids = [row[0] for row in merchants]

    inserted_rows: list[sqlite3.Row] = []
    calendar_id_value = identity_rng.getrandbits(128)
    calendar_event_id = f"calendar-{calendar_id_value:032x}" if publish_calendar else None
    hourly_weights = config.transactions.hourly_transaction_weights.value
    authorization_delay = timedelta(seconds=config.transactions.authorization_delay_seconds.value)
    settlement_delay = timedelta(minutes=config.transactions.settlement_delay_minutes.value)

    with _paired_savepoints(operational, private):
        for transaction_index in range(additional_transaction_count):
            hour = traffic_rng.choices(range(24), weights=hourly_weights, k=1)[0]
            second = traffic_rng.randrange(60)
            local_time = datetime.combine(
                business_date,
                time(hour, traffic_rng.randrange(60), second or 1),
            ).replace(tzinfo=ZoneInfo(config.timeline.business_timezone.value))
            created_at = local_time.astimezone(UTC)
            customer_id = traffic_rng.choice(customers)[0]
            available_merchants = merchant_ids
            if account_roles.get(customer_id) == "household_business":
                suppliers = set(supplier_merchants.get(customer_id, []))
                available_merchants = [item for item in merchant_ids if item in suppliers]
                if not available_merchants:
                    raise ValueError(
                        f"seasonal household {customer_id} has no available supplier merchant"
                    )
            merchant_id = traffic_rng.choice(available_merchants)
            amount = traffic_rng.randint(
                config.transactions.minimum_amount.value,
                config.transactions.maximum_amount.value,
            )
            legacy_random_key = f"txn-{identity_rng.getrandbits(128):032x}"
            txn_id = transaction_id(
                run_seed,
                "p1.incidents.seasonal.transaction",
                episode_id,
                incident_truth_id,
                business_date.isoformat(),
                transaction_index,
            )
            if transaction_random_keys is not None:
                transaction_random_keys[txn_id] = legacy_random_key
            insert_operational_record(
                operational,
                "transactions",
                {
                    "txn_id": txn_id,
                    "episode_id": episode_id,
                    "customer_id": customer_id,
                    "merchant_id": merchant_id,
                    "recipient_customer_id": None,
                    "device_id": device_id_for_transaction(
                        identity_seed=config.seed.value,
                        draw_seed=run_seed,
                        episode_id=episode_id,
                        customer_id=customer_id,
                        transaction_key=legacy_random_key,
                        change_probability=config.world.legitimate_device_change_probability.value,
                    ),
                    "card_token": card_token_for_customer(config.seed.value, customer_id)
                    if channels[0] == "card"
                    else None,
                    "created_at": created_at,
                    "event_at": created_at,
                    "available_at": created_at,
                    "amount": amount,
                    "channel": channels[0],
                },
            )
            authorized_at = created_at + authorization_delay
            settled_at = authorized_at + settlement_delay
            insert_operational_record(
                operational,
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
                operational,
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
            _insert_background_adjustment(
                operational,
                config,
                seed=run_seed,
                episode_id=episode_id,
                txn_id=txn_id,
                random_key=legacy_random_key,
                amount=amount,
                approved=True,
                settled_at=settled_at,
            )
            inserted_rows.append(
                operational.execute(
                    """
                    SELECT txn_id, episode_id, event_at, available_at, customer_id,
                           merchant_id, channel
                    FROM transactions WHERE txn_id = ? AND episode_id = ?
                    """,
                    (txn_id, episode_id),
                ).fetchone()
            )

        if publish_calendar:
            insert_operational_record(
                operational,
                "business_calendar",
                {
                    "calendar_event_id": calendar_event_id,
                    "episode_id": episode_id,
                    "event_at": event_at,
                    "available_at": event_at,
                    "business_date": business_date.isoformat(),
                    "event_name": event_name,
                    "lag_days": 0,
                    "expected_effect_json": {
                        "txn_count": {
                            "aggregation": "sum",
                            "relative_change": float(expected_relative_change),
                        }
                    },
                    "scope_json": {"channel": channels},
                },
            )

        affected_customers = {row["customer_id"] for row in inserted_rows}
        segments = [
            row[0]
            for row in operational.execute(
                f"SELECT DISTINCT segment FROM customers WHERE customer_id IN ({','.join('?' for _ in affected_customers)})",
                tuple(sorted(affected_customers)),
            ).fetchall()
        ]
        effect_available_at = _write_truth(
            private,
            incident_truth_id=incident_truth_id,
            episode_id=episode_id,
            cause="seasonal",
            requires_intervention=False,
            mechanism="one_day_valid_transaction_increase",
            rule_covered=rule_covered,
            affected_entities=[
                row["customer_id"] for row in inserted_rows
            ] + [row["merchant_id"] for row in inserted_rows],
            affected_segments=segments,
            affected_features=["txn_count"],
            affected_rows=inserted_rows,
            accepted_actions=[],
            parent_scenario=parent_scenario,
        )

    return IncidentSummary(
        incident_truth_id=incident_truth_id,
        affected_transaction_ids=tuple(row["txn_id"] for row in inserted_rows),
        effect_available_at=effect_available_at,
        calendar_event_id=calendar_event_id,
    )
