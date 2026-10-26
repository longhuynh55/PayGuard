"""P1.6 round-one operational incident injectors and private truth."""

from __future__ import annotations

import math
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Iterator, Literal
from zoneinfo import ZoneInfo

from simulator.config import SimulatorConfig
from simulator.randomness import keyed_rng
from simulator.storage import insert_operational_record, insert_private_record, utc_timestamp


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


def _utc_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


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
) -> str:
    event_times = [row["event_at"] for row in affected_rows]
    available_times = [row["available_at"] for row in affected_rows]
    effect_available_at = min(available_times)
    intervention_start = _utc_datetime(effect_available_at)
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
            "parent_scenario": None,
        },
    )
    return effect_available_at


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
               channel
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

        customer_ids = {row["customer_id"] for row in changed}
        merchant_ids = {row["merchant_id"] for row in changed}
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
    if business_date <= config.timeline.t0_business_date.value:
        raise ValueError("seasonal event must occur after T0")
    if business_date >= config.timeline.end_business_date_exclusive.value:
        raise ValueError("seasonal event must fall within the configured timeline")

    run_seed = config.seed.value if seed is None else seed
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
    customers = operational.execute(
        """
        SELECT customer_id FROM customers
        WHERE episode_id IN ('base', ?) AND available_at <= ?
        ORDER BY customer_id
        """,
        (episode_id, utc_timestamp(event_at)),
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

    inserted_rows: list[sqlite3.Row] = []
    calendar_event_id = f"calendar-{identity_rng.getrandbits(128):032x}"
    hourly_weights = config.transactions.hourly_transaction_weights.value
    authorization_delay = timedelta(seconds=config.transactions.authorization_delay_seconds.value)
    settlement_delay = timedelta(minutes=config.transactions.settlement_delay_minutes.value)

    with _paired_savepoints(operational, private):
        for _ in range(additional_transaction_count):
            hour = traffic_rng.choices(range(24), weights=hourly_weights, k=1)[0]
            local_time = datetime.combine(
                business_date,
                time(hour, traffic_rng.randrange(60), traffic_rng.randrange(60)),
            ).replace(tzinfo=ZoneInfo(config.timeline.business_timezone.value))
            created_at = local_time.astimezone(UTC)
            customer_id = traffic_rng.choice(customers)[0]
            merchant_id = traffic_rng.choice(merchants)[0]
            amount = traffic_rng.randint(
                config.transactions.minimum_amount.value,
                config.transactions.maximum_amount.value,
            )
            txn_id = f"txn-{identity_rng.getrandbits(128):032x}"
            insert_operational_record(
                operational,
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
            rule_covered=True,
            affected_entities=[
                row["customer_id"] for row in inserted_rows
            ] + [row["merchant_id"] for row in inserted_rows],
            affected_segments=segments,
            affected_features=["txn_count"],
            affected_rows=inserted_rows,
            accepted_actions=[],
        )

    return IncidentSummary(
        incident_truth_id=incident_truth_id,
        affected_transaction_ids=tuple(row["txn_id"] for row in inserted_rows),
        effect_available_at=effect_available_at,
        calendar_event_id=calendar_event_id,
    )
