"""Generate delayed observed labels and point-in-time label snapshots."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from statistics import fmean
from typing import Any, Literal
from zoneinfo import ZoneInfo

from simulator.asof import read_as_of
from simulator.config import SimulatorConfig
from simulator.fraud import card_testing_metadata_key
from simulator.randomness import keyed_rng
from simulator.storage import insert_operational_record, insert_private_record, utc_timestamp


LABEL_TRUTH_METADATA_KEY = "p1.5.label_truth.v1"
LABEL_POLICY_VERSION = "mvp-h90-v1"
SnapshotState = Literal[
    "confirmed_fraud", "confirmed_legit", "matured_negative", "pending"
]


@dataclass(frozen=True)
class LabelSummary:
    transaction_count: int
    label_event_count: int
    last_transaction_at: str
    label_tail_end_at: str


@dataclass(frozen=True)
class LabelSnapshot:
    txn_id: str
    episode_id: str
    event_at: str
    state: SnapshotState
    label: str | None
    label_event_id: str | None


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _t0_timestamp(config: SimulatorConfig) -> datetime:
    business_timezone = ZoneInfo(config.timeline.business_timezone.value)
    local_t0 = datetime.combine(
        config.timeline.t0_business_date.value, time.min
    ).replace(tzinfo=business_timezone)
    return local_t0.astimezone(UTC)


def label_truth_metadata_key(episode_id: str) -> str:
    """Return the private label-truth metadata key for one episode stream."""
    if episode_id == "base":
        return LABEL_TRUTH_METADATA_KEY
    return f"{LABEL_TRUTH_METADATA_KEY}:{episode_id}"


def _load_card_testing_fraud_ids(
    private: sqlite3.Connection, episode_id: str
) -> set[str]:
    row = private.execute(
        "SELECT value_json FROM run_metadata WHERE key = ?",
        (card_testing_metadata_key(episode_id),),
    ).fetchone()
    if row is None:
        raise ValueError("P1.4 card-testing truth metadata is required before label generation")
    metadata = json.loads(row[0])
    if not isinstance(metadata, dict) or metadata.get("mechanism") != "card_testing":
        raise ValueError("P1.4 card-testing truth metadata is malformed")

    fraud_ids: set[str] = set()
    for group in metadata.get("groups", []):
        if group.get("role") != "fraud":
            continue
        fraud_ids.update(group.get("small_transaction_ids", []))
        followup_id = group.get("larger_followup_transaction_id")
        if followup_id is not None:
            fraud_ids.add(followup_id)
    return fraud_ids


def _source_for_channel(channel: str) -> str:
    """Choose a source supported by the observed transaction channel."""
    return "chargeback" if channel == "card" else "customer_report"


def generate_label_events(
    operational: sqlite3.Connection,
    private: sqlite3.Connection,
    config: SimulatorConfig,
    *,
    seed: int | None = None,
    episode_id: str = "base",
) -> LabelSummary:
    """Append reported labels for transactions in one base or episode stream.

    Reporting and delay draws use label-specific keyed RNG streams. True labels
    and transactions that are never reported are recorded only in private
    ``run_metadata``; no truth fields are added to operational tables.
    """
    if operational is private:
        raise ValueError("label generation requires separate operational and private connections")
    run_seed = config.seed.value if seed is None else seed
    if isinstance(run_seed, bool) or not isinstance(run_seed, int) or run_seed < 0:
        raise ValueError("label seed must be a non-negative integer")
    if not isinstance(episode_id, str) or not episode_id.strip():
        raise ValueError("label generation requires a non-empty episode_id")
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
            raise ValueError("label episode must be registered in private storage")
    if registered is None:
        raise ValueError("label generation requires a registered episode")
    if operational.execute(
        "SELECT 1 FROM label_events WHERE episode_id = ? LIMIT 1", (episode_id,)
    ).fetchone():
        raise ValueError("label generation requires an empty episode label stream")
    metadata_key = label_truth_metadata_key(episode_id)
    if private.execute(
        "SELECT 1 FROM run_metadata WHERE key = ?", (metadata_key,)
    ).fetchone():
        raise ValueError("label generation has already been recorded in this private export")

    transactions = [
        dict(row)
        for row in operational.execute(
            "SELECT txn_id, episode_id, event_at, channel FROM transactions "
            "WHERE episode_id = ? ORDER BY event_at, txn_id",
            (episode_id,),
        )
    ]
    if not transactions:
        raise ValueError("P1.3/P1.4 transactions must exist before label generation")
    fraud_ids = _load_card_testing_fraud_ids(private, episode_id)
    txn_ids = {row["txn_id"] for row in transactions}
    if not fraud_ids <= txn_ids:
        missing = sorted(fraud_ids - txn_ids)
        raise ValueError(f"card-testing truth references missing transactions: {missing[:3]}")
    for row in transactions:
        if row["txn_id"] in fraud_ids and row["channel"] != "card":
            raise ValueError("card-testing fraud truth must reference card-channel transactions")

    label_config = config.label_reporting
    report_probability = label_config.probability_reported.value
    minimum_delay = label_config.minimum_delay_days.value
    maximum_delay = label_config.maximum_delay_days.value
    horizon_days = label_config.maturity_horizon_days.value
    last_transaction_at = max(_parse_timestamp(row["event_at"]) for row in transactions)
    tail_days = max(horizon_days, maximum_delay)
    label_tail_end_at = last_transaction_at + timedelta(days=tail_days)

    truth_outcomes: list[dict[str, Any]] = []
    label_event_count = 0

    operational.execute("SAVEPOINT p1_5_label_events")
    private.execute("SAVEPOINT p1_5_label_truth")
    try:
        for transaction in transactions:
            txn_id = transaction["txn_id"]
            true_label = "fraud" if txn_id in fraud_ids else "legit"
            report_rng = keyed_rng(run_seed, "p1.labels.reporting", txn_id)
            will_report = report_rng.random() < report_probability
            delay_days: int | None = None
            source: str | None = None
            reported_at: str | None = None
            available_at: str | None = None

            if will_report:
                delay_rng = keyed_rng(run_seed, "p1.labels.delay_days", txn_id)
                delay_days = delay_rng.randint(minimum_delay, maximum_delay)
                reported_at = utc_timestamp(
                    _parse_timestamp(transaction["event_at"])
                    + timedelta(days=delay_days)
                )
                # P1.5 does not model a separate source-ingestion lag.
                available_at = reported_at
                source = _source_for_channel(transaction["channel"])
                insert_operational_record(
                    operational,
                    "label_events",
                    {
                        "event_id": f"label-{txn_id}",
                        "txn_id": txn_id,
                        "episode_id": transaction["episode_id"],
                        "event_at": transaction["event_at"],
                        "label": true_label,
                        "label_status": "confirmed",
                        "reported_at": reported_at,
                        "available_at": available_at,
                        "source": source,
                        "supersedes_event_id": None,
                    },
                )
                label_event_count += 1

            truth_outcomes.append(
                {
                    "txn_id": txn_id,
                    "episode_id": transaction["episode_id"],
                    "event_at": utc_timestamp(transaction["event_at"]),
                    "true_label": true_label,
                    "reported": will_report,
                    "report_delay_days": delay_days,
                    "reported_at": reported_at,
                    "available_at": available_at,
                    "source": source,
                }
            )

        insert_private_record(
            private,
            "run_metadata",
            {
                "key": metadata_key,
                "value_json": {
                    "algorithm": "p1.5-label-events-v1",
                    "episode_id": episode_id,
                    "seed": run_seed,
                    "maturity_horizon_days": horizon_days,
                    "minimum_delay_days": minimum_delay,
                    "maximum_delay_days": maximum_delay,
                    "delay_distribution": label_config.delay_distribution.value,
                    "probability_reported": report_probability,
                    "last_transaction_at": utc_timestamp(last_transaction_at),
                    "label_tail_end_at": utc_timestamp(label_tail_end_at),
                    "transactions": truth_outcomes,
                },
            },
        )
        operational.execute("RELEASE SAVEPOINT p1_5_label_events")
        private.execute("RELEASE SAVEPOINT p1_5_label_truth")
    except Exception:
        operational.execute("ROLLBACK TO SAVEPOINT p1_5_label_events")
        operational.execute("RELEASE SAVEPOINT p1_5_label_events")
        private.execute("ROLLBACK TO SAVEPOINT p1_5_label_truth")
        private.execute("RELEASE SAVEPOINT p1_5_label_truth")
        raise

    return LabelSummary(
        transaction_count=len(transactions),
        label_event_count=label_event_count,
        last_transaction_at=utc_timestamp(last_transaction_at),
        label_tail_end_at=utc_timestamp(label_tail_end_at),
    )


def snapshot_labels(
    operational: sqlite3.Connection,
    config: SimulatorConfig,
    *,
    episode_id: str,
    as_of_time: datetime | str,
) -> list[LabelSnapshot]:
    """Return the four MVP label states using the existing as-of reader."""
    normalized_as_of = utc_timestamp(as_of_time)
    as_of = _parse_timestamp(normalized_as_of)
    t0 = _t0_timestamp(config)
    transactions = read_as_of(
        operational,
        "transactions",
        episode_id=episode_id,
        t0=t0,
        as_of_time=normalized_as_of,
    )
    labels = read_as_of(
        operational,
        "label_events",
        episode_id=episode_id,
        t0=t0,
        as_of_time=normalized_as_of,
    )
    labels_by_txn: dict[str, dict[str, Any]] = {}
    for event in labels:
        if event["txn_id"] in labels_by_txn:
            raise ValueError(f"multiple label conclusions found for transaction {event['txn_id']!r}")
        labels_by_txn[event["txn_id"]] = event

    horizon = timedelta(days=config.label_reporting.maturity_horizon_days.value)
    snapshots: list[LabelSnapshot] = []
    for transaction in transactions:
        event = labels_by_txn.get(transaction["txn_id"])
        if event is not None and event["label"] == "fraud":
            state: SnapshotState = "confirmed_fraud"
            label: str | None = "fraud"
        elif event is not None:
            state = "confirmed_legit"
            label = "legit"
        elif as_of - _parse_timestamp(transaction["event_at"]) >= horizon:
            state = "matured_negative"
            label = None
        else:
            state = "pending"
            label = None
        snapshots.append(
            LabelSnapshot(
                txn_id=transaction["txn_id"],
                episode_id=transaction["episode_id"],
                event_at=transaction["event_at"],
                state=state,
                label=label,
                label_event_id=event["event_id"] if event is not None else None,
            )
        )
    return snapshots


def label_delay_report(
    operational: sqlite3.Connection,
    private: sqlite3.Connection,
    config: SimulatorConfig,
    *,
    episode_id: str,
    as_of_time: datetime | str,
) -> dict[str, Any]:
    """Report observable snapshot delays separately from private oracle outcomes."""
    normalized_as_of = utc_timestamp(as_of_time)
    as_of = _parse_timestamp(normalized_as_of)
    t0 = _t0_timestamp(config)
    snapshots = snapshot_labels(
        operational,
        config,
        episode_id=episode_id,
        as_of_time=normalized_as_of,
    )
    visible_txn_ids = {snapshot.txn_id for snapshot in snapshots}
    visible_labels = [
        event
        for event in read_as_of(
            operational,
            "label_events",
            episode_id=episode_id,
            t0=t0,
            as_of_time=normalized_as_of,
        )
        if event["txn_id"] in visible_txn_ids
    ]
    state_counts = {
        state: sum(snapshot.state == state for snapshot in snapshots)
        for state in ("confirmed_fraud", "confirmed_legit", "matured_negative", "pending")
    }
    visible_delay_days = [
        (_parse_timestamp(event["reported_at"]) - _parse_timestamp(event["event_at"])).total_seconds()
        / 86400
        for event in visible_labels
    ]
    observed = {
        "basis": "operational_label_snapshot",
        "transaction_count": len(snapshots),
        "snapshot_state_counts": state_counts,
        "available_label_event_count": len(visible_labels),
        "available_label_delay_days": {
            "count": len(visible_delay_days),
            "mean": fmean(visible_delay_days) if visible_delay_days else None,
            "minimum": min(visible_delay_days) if visible_delay_days else None,
            "maximum": max(visible_delay_days) if visible_delay_days else None,
        },
        "censoring": {
            "right_censored_pending_count": state_counts["pending"],
            "policy_horizon_censored_matured_negative_count": state_counts["matured_negative"],
            "unlabeled_at_snapshot_count": (
                state_counts["pending"] + state_counts["matured_negative"]
            ),
        },
    }

    base_metadata_row = private.execute(
        "SELECT value_json FROM run_metadata WHERE key = ?",
        (label_truth_metadata_key("base"),),
    ).fetchone()
    if base_metadata_row is None:
        raise ValueError("private P1.5 label truth metadata is required for oracle delay reporting")
    base_metadata = json.loads(base_metadata_row[0])
    metadata_records = [base_metadata]
    episode_txn_ids = {
        snapshot.txn_id for snapshot in snapshots if snapshot.episode_id == episode_id
    }
    if episode_id != "base":
        episode_metadata_row = private.execute(
            "SELECT value_json FROM run_metadata WHERE key = ?",
            (label_truth_metadata_key(episode_id),),
        ).fetchone()
        if episode_metadata_row is None:
            if episode_txn_ids:
                raise ValueError(
                    "private P1.5 label truth metadata is required for the observed episode snapshot"
                )
        else:
            episode_metadata = json.loads(episode_metadata_row[0])
            metadata_records.append(episode_metadata)

    metadata = base_metadata
    for metadata_record in metadata_records[1:]:
        if any(
            metadata_record[field] != metadata[field]
            for field in (
                "maturity_horizon_days",
                "maximum_delay_days",
            )
        ):
            raise ValueError("base and episode label truth metadata use different reporting policies")
    horizon_days = metadata["maturity_horizon_days"]
    outcomes_by_txn: dict[str, dict[str, Any]] = {}
    for metadata_record in metadata_records:
        for outcome in metadata_record["transactions"]:
            txn_id = outcome["txn_id"]
            if txn_id not in visible_txn_ids:
                continue
            if txn_id in outcomes_by_txn:
                raise ValueError(f"private label truth duplicates transaction {txn_id!r}")
            outcomes_by_txn[txn_id] = outcome
    outcomes = list(outcomes_by_txn.values())
    if len(outcomes) != len(visible_txn_ids):
        raise ValueError("private P1.5 truth metadata does not cover the observed transaction snapshot")
    fraud_outcomes = [outcome for outcome in outcomes if outcome["true_label"] == "fraud"]
    reported_fraud = [outcome for outcome in fraud_outcomes if outcome["reported"]]
    reported_fraud_delays = [outcome["report_delay_days"] for outcome in reported_fraud]
    reported_not_available = [
        outcome
        for outcome in outcomes
        if outcome["reported"] and _parse_timestamp(outcome["available_at"]) > as_of
    ]
    selected_last_transaction_at = max(
        (_parse_timestamp(outcome["event_at"]) for outcome in outcomes),
        default=None,
    )
    tail_days = max(
        horizon_days,
        metadata["maximum_delay_days"],
    )
    selected_tail_end = (
        selected_last_transaction_at + timedelta(days=tail_days)
        if selected_last_transaction_at is not None
        else None
    )
    oracle = {
        "basis": "private_simulator_truth",
        "transaction_count": len(outcomes),
        "true_fraud_count": len(fraud_outcomes),
        "fraud_reported_count": len(reported_fraud),
        "fraud_never_reported_count": len(fraud_outcomes) - len(reported_fraud),
        "fraud_reported_within_horizon_count": sum(
            outcome["report_delay_days"] <= horizon_days for outcome in reported_fraud
        ),
        "fraud_reported_after_horizon_count": sum(
            outcome["report_delay_days"] > horizon_days for outcome in reported_fraud
        ),
        "fraud_report_delay_days": {
            "count": len(reported_fraud_delays),
            "mean": fmean(reported_fraud_delays) if reported_fraud_delays else None,
            "minimum": min(reported_fraud_delays) if reported_fraud_delays else None,
            "maximum": max(reported_fraud_delays) if reported_fraud_delays else None,
        },
        "eventual_reports_not_available_at_snapshot_count": len(reported_not_available),
        "label_tail_end_at": utc_timestamp(selected_tail_end) if selected_tail_end else None,
        "label_tail_complete_at_snapshot": (
            as_of >= selected_tail_end if selected_tail_end is not None else False
        ),
        "censoring": {
            "eventual_reports_not_yet_available_count": len(reported_not_available),
            "tail_remaining_seconds": (
                max(timedelta(0), selected_tail_end - as_of).total_seconds()
                if selected_tail_end is not None
                else None
            ),
        },
    }
    return {
        "as_of_time": normalized_as_of,
        "label_policy_version": LABEL_POLICY_VERSION,
        "model_version": None,
        "maturity_horizon_days": config.label_reporting.maturity_horizon_days.value,
        "observed": observed,
        "oracle": oracle,
    }
