"""Build smoke support reports from operational and private simulator stores."""

from __future__ import annotations

from bisect import bisect_right, insort
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from model.config import load_pipeline_config
from model.data import InsufficientSplitSupportError, ModelConfig, build_splits, load_model_config
from simulator.asof import read_as_of
from simulator.common import parse_timestamp
from simulator.config import EpisodeConfig, SimulatorConfig
from simulator.labels import label_delay_report, label_truth_metadata_key, snapshot_labels
from simulator.storage import utc_timestamp


REFERENCE_ROW_RE = re.compile(
    r"^\|\s*Reference drift PSI/KS[^|]*\|\s*\[(\d{2}/\d{2}/\d{4}),\s*"
    r"(\d{2}/\d{2}/\d{4})\)\s*\|$",
    re.MULTILINE,
)
WINDOWS = (("1h", timedelta(hours=1)), ("24h", timedelta(hours=24)), ("7d", timedelta(days=7)))
REPORT_PROFILES = ("smoke", "pilot")
LABEL_STATES = ("confirmed_fraud", "confirmed_legit", "matured_negative", "pending")


def _t0_timestamp(config: SimulatorConfig, timezone_name: str) -> datetime:
    return datetime.combine(
        config.timeline.t0_business_date.value,
        time.min,
        tzinfo=ZoneInfo(timezone_name),
    ).astimezone(UTC)


def _business_date(value: str, timezone_name: str) -> date:
    return parse_timestamp(value).astimezone(ZoneInfo(timezone_name)).date()


def _date_interval_from_model_doc(path: str | Path) -> tuple[date, date]:
    source = Path(path).read_text(encoding="utf-8")
    match = REFERENCE_ROW_RE.search(source)
    if match is None:
        raise ValueError(
            f"{path} must define the Reference drift PSI/KS interval as [DD/MM/YYYY, DD/MM/YYYY)"
        )
    start = datetime.strptime(match.group(1), "%d/%m/%Y").date()
    end = datetime.strptime(match.group(2), "%d/%m/%Y").date()
    if start >= end:
        raise ValueError("model reference interval must have a non-empty date range")
    return start, end


def _split_dict(model_config: ModelConfig) -> dict[str, dict[str, str]]:
    return {
        name: {
            "start_inclusive": window.start_inclusive.isoformat(),
            "end_exclusive": window.end_exclusive.isoformat(),
        }
        for name, window in model_config.windows()
    }


def resolved_support_config(
    model_config: ModelConfig,
    model_config_path: str | Path,
    model_doc_path: str | Path,
) -> dict[str, Any]:
    """Return the split/report settings resolved from the locked P2 sources."""
    pipeline = load_pipeline_config(model_config_path)
    reference_start, reference_end = _date_interval_from_model_doc(model_doc_path)
    return {
        "business_timezone": model_config.business_timezone,
        "split_provenance": model_config.provenance,
        "split_source": str(model_config_path),
        "splits": _split_dict(model_config),
        "reference_source": str(model_doc_path),
        "reference_drift": {
            "start_inclusive": reference_start.isoformat(),
            "end_exclusive": reference_end.isoformat(),
        },
        "calibration_support": {
            "minimum_support": pipeline.calibration.minimum_support,
            "support_basis": pipeline.calibration.support_basis,
            "method": pipeline.calibration.method,
            "fallback_method": pipeline.calibration.fallback_method,
        },
    }


def _audit_support(
    operational: sqlite3.Connection,
    simulator_config: SimulatorConfig,
    model_config: ModelConfig,
    model_config_path: str | Path,
    model_doc_path: str | Path,
    episode_id: str,
) -> dict[str, Any]:
    try:
        splits = build_splits(
            operational,
            simulator_config,
            episode_id=episode_id,
            model_config=model_config,
        )
        audit = splits.audit
    except InsufficientSplitSupportError as exc:
        audit = exc.report

    pipeline = load_pipeline_config(model_config_path)
    cohorts: dict[str, Any] = {}
    for item in audit.splits:
        missing: list[str] = []
        warnings: list[str] = []
        if item.positive_count == 0:
            missing.append("positive")
        if item.negative_count == 0:
            missing.append("negative")
        requirements: dict[str, Any] = {"minimum_examples_per_class": 1}
        calibration_met: bool | None = None
        if item.name == "calibration":
            required = pipeline.calibration.minimum_support
            requirements["calibration_minimum_per_class"] = required
            calibration_met = (
                item.positive_count >= required and item.negative_count >= required
            )
            if not calibration_met:
                warnings.append(
                    f"isotonic_support_below_{required}_per_class; "
                    f"fallback_method={pipeline.calibration.fallback_method}"
                )
        cohorts[item.name] = {
            "date_window": {
                "start_inclusive": item.window.start_inclusive.isoformat(),
                "end_exclusive": item.window.end_exclusive.isoformat(),
            },
            "candidate_count": item.candidate_count,
            "age_eligible_count": item.age_eligible_count,
            "under_horizon_excluded_count": item.under_horizon_excluded_count,
            "pending_excluded_count": item.pending_excluded_count,
            "sample_count": item.sample_count,
            "positive_count": item.positive_count,
            "negative_count": item.negative_count,
            "label_source": "operational_label_snapshot",
            "requirements": requirements,
            "calibration_minimum_met": calibration_met,
            "missing_support": missing,
            "warnings": warnings,
            "support_status": "sufficient" if not missing else "insufficient",
        }

    return {
        "source_episode_id": episode_id,
        "as_of_time": audit.as_of_time,
        "horizon_days": audit.horizon_days,
        "label_policy_version": audit.label_policy_version,
        "cohorts": cohorts,
        "support_status": (
            "sufficient"
            if all(item["support_status"] == "sufficient" for item in cohorts.values())
            else "insufficient"
        ),
    }


def _distribution(rows: list[dict[str, Any]], timezone_name: str) -> dict[str, Any]:
    by_channel = Counter(str(row["channel"]) for row in rows)
    by_date = Counter(_business_date(row["event_at"], timezone_name).isoformat() for row in rows)
    by_hour = Counter(
        f"{parse_timestamp(row['event_at']).astimezone(ZoneInfo(timezone_name)).hour:02d}"
        for row in rows
    )
    return {
        "transaction_count": len(rows),
        "by_channel": dict(sorted(by_channel.items())),
        "by_business_date": dict(sorted(by_date.items())),
        "by_local_hour": dict(sorted(by_hour.items())),
    }


def _completeness(
    transactions: list[dict[str, Any]],
    customers: list[dict[str, Any]],
    *,
    episode_id: str,
) -> dict[str, Any]:
    customers_by_id = {row["customer_id"]: row for row in customers}
    result: dict[str, Any] = {"transaction_count": len(transactions), "windows": {}}
    for name, duration in WINDOWS:
        complete_count = 0
        for transaction in transactions:
            customer = customers_by_id.get(transaction["customer_id"])
            created_at = parse_timestamp(transaction["created_at"])
            if customer is None or customer["episode_id"] not in {"base", episode_id}:
                continue
            if parse_timestamp(customer["available_at"]) > created_at:
                continue
            observed_from = max(
                parse_timestamp(customer["event_at"]),
                parse_timestamp(customer["available_at"]),
            )
            if observed_from <= created_at - duration:
                complete_count += 1
        count = len(transactions)
        result["windows"][name] = {
            "complete_count": complete_count,
            "censored_count": count - complete_count,
            "completeness_fraction": round(complete_count / count, 6) if count else None,
        }
    return result


def _truth_counts_for_window(
    private: sqlite3.Connection,
    *,
    episode_id: str,
    start: date,
    end: date,
    timezone_name: str,
) -> dict[str, int]:
    key = label_truth_metadata_key(episode_id)
    row = private.execute("SELECT value_json FROM run_metadata WHERE key = ?", (key,)).fetchone()
    if row is None:
        raise ValueError(f"private label truth metadata is missing for {episode_id!r}")
    metadata = json.loads(row[0])
    outcomes = [
        outcome
        for outcome in metadata["transactions"]
        if start <= _business_date(outcome["event_at"], timezone_name) < end
    ]
    fraud_count = sum(outcome["true_label"] == "fraud" for outcome in outcomes)
    return {
        "transaction_count": len(outcomes),
        "truth_positive_count": fraud_count,
        "truth_negative_count": len(outcomes) - fraud_count,
        "reported_count": sum(bool(outcome["reported"]) for outcome in outcomes),
    }


def _auc(scores: list[float], labels: list[int]) -> float | None:
    positives = sum(labels)
    negatives = len(labels) - positives
    if not positives or not negatives:
        return None
    ordered = sorted(zip(scores, labels, strict=True), key=lambda item: item[0])
    positive_rank_sum = 0.0
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][0] == ordered[index][0]:
            end += 1
        average_rank = ((index + 1) + end) / 2
        positive_rank_sum += average_rank * sum(label for _, label in ordered[index:end])
        index = end
    return round(
        (positive_rank_sum - positives * (positives + 1) / 2) / (positives * negatives),
        6,
    )


_P14_INF = float("inf")


def _entries_between(
    entries: Sequence[tuple[Any, ...]],
    low_exclusive: datetime,
    high_inclusive: datetime,
) -> Sequence[tuple[Any, ...]]:
    """Return sorted index entries whose first field lies in (low, high]."""
    start = bisect_right(entries, (low_exclusive, _P14_INF))
    end = bisect_right(entries, (high_inclusive, _P14_INF))
    return entries[start:end]


def _p14_point_in_time_features(
    rows: Sequence[dict[str, Any]],
    *,
    target_ids: set[str],
    customers: Mapping[str, Mapping[str, Any]],
    near_min: int,
    near_max: int,
) -> dict[str, dict[str, float]]:
    """Return the P1.4 point-in-time features of every target transaction.

    A row belongs to a target's history when its ``created_at`` and ``available_at`` are
    both at or before the target's ``created_at`` (text order, as in the per-target scan
    this replaces). Targets are swept in ``created_at`` order and each row is indexed once,
    when that point is reached. Trailing windows are bisect slices over sorted per-key
    entries, and per-account counters answer the seen-before questions. The target is
    removed from its own prior history by position, so it still counts toward the windows
    exactly when it is part of its history.
    """
    created = [parse_timestamp(row["created_at"]) for row in rows]
    entering_at = [max(row["created_at"], row["available_at"]) for row in rows]
    pending = sorted(range(len(rows)), key=entering_at.__getitem__)
    device_counts: Counter[tuple[str, str | None]] = Counter()
    pair_counts: Counter[tuple[str, str]] = Counter()
    senders_by_recipient: defaultdict[str, list[tuple[datetime, int, str]]] = defaultdict(list)
    outflows_by_customer: defaultdict[str, list[tuple[datetime, int, int]]] = defaultdict(list)
    inflows_by_recipient: defaultdict[str, list[tuple[datetime, int, int]]] = defaultdict(list)
    activity_by_account: defaultdict[str, list[tuple[datetime, int]]] = defaultdict(list)

    def admit(index: int) -> None:
        row = rows[index]
        customer = row["customer_id"]
        recipient = row["recipient_customer_id"]
        amount = row["amount"]
        device_counts[(customer, row["device_id"])] += 1
        insort(activity_by_account[customer], (created[index], index))
        if recipient is None:
            return
        pair_counts[(customer, recipient)] += 1
        insort(senders_by_recipient[recipient], (created[index], index, customer))
        if recipient != customer:
            insort(activity_by_account[recipient], (created[index], index))
        if type(amount) is int:
            insort(outflows_by_customer[customer], (created[index], index, amount))
            insort(inflows_by_recipient[recipient], (created[index], index, amount))

    features: dict[str, dict[str, float]] = {}
    targets = sorted(
        (index for index, row in enumerate(rows) if row["txn_id"] in target_ids),
        key=lambda index: rows[index]["created_at"],
    )
    admitted = 0
    for index in targets:
        target = rows[index]
        moment = target["created_at"]
        while admitted < len(pending) and entering_at[pending[admitted]] <= moment:
            admit(pending[admitted])
            admitted += 1
        in_history = entering_at[index] <= moment
        own = 1 if in_history else 0
        current_at = created[index]
        customer = target["customer_id"]
        recipient_id = target["recipient_customer_id"]
        is_transfer = recipient_id is not None
        device_seen = device_counts[(customer, target["device_id"])] - own > 0
        pair_seen = is_transfer and pair_counts[(customer, recipient_id)] - own > 0
        cutoff_7d = current_at - timedelta(days=7)
        senders = (
            {
                sender_id
                for _, _, sender_id in _entries_between(
                    senders_by_recipient.get(recipient_id, []), cutoff_7d, current_at
                )
            }
            if is_transfer
            else set()
        )
        fan_in = len(senders)
        older_senders = sum(
            customers.get(sender_id, {}).get("age_band") == "55+" for sender_id in senders
        )
        cutoff_24h = current_at - timedelta(hours=24)
        outgoing = sum(
            amount
            for _, _, amount in _entries_between(
                outflows_by_customer.get(customer, []), cutoff_24h, current_at
            )
        )
        incoming = sum(
            amount
            for _, _, amount in _entries_between(
                inflows_by_recipient.get(customer, []), cutoff_24h, current_at
            )
        )
        last_prior_activity: datetime | None = None
        if is_transfer:
            for activity_at, other_index in reversed(
                activity_by_account.get(recipient_id, [])
            ):
                if other_index != index:
                    last_prior_activity = activity_at
                    break
        recipient = customers.get(recipient_id) if is_transfer else None
        recipient_created = parse_timestamp(recipient["event_at"]) if recipient else None
        sender = customers.get(customer)
        sender_created = parse_timestamp(sender["event_at"]) if sender else None
        sender_age_days = (
            (current_at - sender_created).total_seconds() / 86_400
            if sender_created is not None
            else -1.0
        )
        recipient_age_days = (
            (current_at - recipient_created).total_seconds() / 86_400
            if recipient_created is not None
            else -1.0
        )
        dormant_cutoff = current_at - timedelta(days=30)
        new_or_dormant = bool(
            is_transfer
            and (
                recipient_created is None
                or recipient_created > dormant_cutoff
                or last_prior_activity is None
                or last_prior_activity <= dormant_cutoff
            )
        )
        amount = target["amount"]
        features[target["txn_id"]] = {
            "new_device_to_sender": float(target["device_id"] is not None and not device_seen),
            "first_transfer_to_counterparty": float(is_transfer and not pair_seen),
            "recipient_fan_in_7d": float(fan_in),
            "older_sender_share_7d": float(older_senders / fan_in if fan_in else 0.0),
            "account_outflow_ratio_24h": float(outgoing / incoming if incoming else 0.0),
            "new_or_dormant_recipient": float(new_or_dormant),
            "sender_account_age_days": sender_age_days,
            "recipient_account_age_days": recipient_age_days,
            "transaction_id_hash_format": float(
                re.fullmatch(r"txn-[0-9a-f]{64}", target["txn_id"]) is not None
            ),
            "customer_id_six_digit_format": float(
                re.fullmatch(r"customer-[0-9]{6}", customer) is not None
            ),
            "amount_near_10m_vnd": float(
                type(amount) is int and near_min <= amount <= near_max
            ),
            "created_at_second_zero": float(current_at.second == 0),
        }
    return features


def _p14_feature_diagnostics(
    operational: sqlite3.Connection,
    private: sqlite3.Connection,
    *,
    episode_id: str,
    config: SimulatorConfig,
) -> dict[str, Any]:
    """Measure point-in-time operational signals against private P1.5 truth."""
    truth_row = private.execute(
        "SELECT value_json FROM run_metadata WHERE key = ?",
        (label_truth_metadata_key(episode_id),),
    ).fetchone()
    if truth_row is None:
        raise ValueError(f"private P1.5 truth is missing for {episode_id!r}")
    outcomes = json.loads(truth_row[0]).get("transactions", [])
    labels_by_id = {
        item["txn_id"]: int(item["true_label"] == "fraud") for item in outcomes
    }
    t0_text = utc_timestamp(_t0_timestamp(config, config.timeline.business_timezone.value))
    # The diagnostic's own transactions plus base history before T0 (all of base for base).
    all_rows = [
        dict(row)
        for row in operational.execute(
            "SELECT * FROM transactions "
            "WHERE episode_id = ? OR (episode_id = 'base' AND event_at < ?) "
            "ORDER BY event_at, txn_id",
            (episode_id, t0_text),
        )
    ]
    customers = {
        row["customer_id"]: dict(row)
        for row in operational.execute("SELECT * FROM customers")
        if row["episode_id"] in {"base", episode_id}
    }
    mechanism_key = (
        "p1.4.mechanisms.v1"
        if episode_id == "base"
        else f"p1.4.mechanisms.v1:{episode_id}"
    )
    mechanism_row = private.execute(
        "SELECT value_json FROM run_metadata WHERE key = ?", (mechanism_key,)
    ).fetchone()
    mechanism_groups = json.loads(mechanism_row[0]).get("groups", []) if mechanism_row else []
    social_transaction_ids: set[str] = set()
    control_transaction_ids: set[str] = set()
    mechanism_transaction_counts: Counter[str] = Counter()
    for group in mechanism_groups:
        mechanism = group.get("mechanism", "unknown")
        transaction_ids = {
            txn_id
            for field in (
                "transaction_ids",
                "incoming_transaction_ids",
                "outgoing_transaction_ids",
            )
            for txn_id in group.get(field, [])
        }
        mechanism_transaction_counts[mechanism] += len(transaction_ids)
        if mechanism == "social_engineering":
            social_transaction_ids.update(transaction_ids)
        elif mechanism == "legit_business_spike":
            control_transaction_ids.update(transaction_ids)
    transactions_by_id = {row["txn_id"]: row for row in all_rows}
    configured_cashout_delay = {
        "collusion": config.fraud.collusion.cashout_delay_minutes.value,
        "social_engineering": config.fraud.social_engineering.cashout_delay_minutes.value,
    }
    fixed_delay_artifacts: dict[str, dict[str, Any]] = {}
    for mechanism in ("collusion", "social_engineering"):
        delays_seconds: list[int] = []
        for group in mechanism_groups:
            if group.get("mechanism") != mechanism:
                continue
            incoming_ids = group.get("incoming_transaction_ids", [])
            outgoing_ids = group.get("outgoing_transaction_ids", [])
            for incoming_id, outgoing_id in zip(incoming_ids, outgoing_ids):
                incoming = transactions_by_id.get(incoming_id)
                outgoing = transactions_by_id.get(outgoing_id)
                if incoming is None or outgoing is None:
                    continue
                delay = parse_timestamp(outgoing["created_at"]) - parse_timestamp(
                    incoming["created_at"]
                )
                delays_seconds.append(int(delay.total_seconds()))
        exact_seconds = configured_cashout_delay[mechanism] * 60
        fixed_delay_artifacts[mechanism] = {
            "configured_delay_minutes": configured_cashout_delay[mechanism],
            "paired_cashout_count": len(delays_seconds),
            "exact_configured_delay_count": sum(
                delay == exact_seconds for delay in delays_seconds
            ),
            "distinct_delay_seconds": sorted(set(delays_seconds)),
            "minimum_delay_seconds": min(delays_seconds) if delays_seconds else None,
            "maximum_delay_seconds": max(delays_seconds) if delays_seconds else None,
        }
    feature_scores: dict[str, list[float]] = {
        "new_device_to_sender": [],
        "first_transfer_to_counterparty": [],
        "recipient_fan_in_7d": [],
        "older_sender_share_7d": [],
        "account_outflow_ratio_24h": [],
        "new_or_dormant_recipient": [],
        "sender_account_age_days": [],
        "recipient_account_age_days": [],
        "transaction_id_hash_format": [],
        "customer_id_six_digit_format": [],
        "amount_near_10m_vnd": [],
        "created_at_second_zero": [],
    }
    target_labels: list[int] = []
    near_min = config.fraud.near_threshold_minimum_amount.value
    near_max = config.fraud.near_threshold_maximum_amount.value
    features_by_id = _p14_point_in_time_features(
        all_rows,
        target_ids=set(labels_by_id),
        customers=customers,
        near_min=near_min,
        near_max=near_max,
    )
    for target in all_rows:
        if target["txn_id"] not in labels_by_id:
            continue
        target_labels.append(labels_by_id[target["txn_id"]])
        for name, value in features_by_id[target["txn_id"]].items():
            feature_scores[name].append(value)
    aucs = {
        feature: _auc(scores, target_labels)
        for feature, scores in feature_scores.items()
    }
    strong = {
        name: auc for name, auc in aucs.items()
        if auc is not None and (auc >= 0.8 or auc <= 0.2)
    }
    explanations: dict[str, str] = {
        "new_device_to_sender": "ATO uses a new device signature probabilistically; the background also contains independently sampled legitimate device changes.",
        "first_transfer_to_counterparty": "The signal is derived from the sender-recipient history available by the transaction time.",
        "recipient_fan_in_7d": "The signal counts distinct senders in the observed seven-day receiving window.",
        "older_sender_share_7d": "The signal uses the operational age bands of distinct senders in the observed seven-day receiving window.",
        "account_outflow_ratio_24h": "The signal compares observed outgoing and incoming transfer amounts in the trailing 24-hour window.",
        "new_or_dormant_recipient": "The signal uses customer availability and prior operational activity, not a private role field.",
        "sender_account_age_days": "Sender age is computed from the operational customer availability time at the transaction timestamp.",
        "recipient_account_age_days": "Recipient age is computed from the operational customer availability time at the transaction timestamp; social-engineering cash-in intentionally uses newly available mule accounts.",
        "transaction_id_hash_format": "Every generated transaction uses the same opaque SHA-256 ID shape, so format alone cannot separate truth labels.",
        "customer_id_six_digit_format": "Every generated customer uses the same six-digit ID shape; the digits are deterministically allocated without encoding account role.",
        "amount_near_10m_vnd": (
            "Non-card-testing fraud amounts are sampled from the configured "
            f"{near_min:,}–{near_max:,} VND band."
        ),
        "created_at_second_zero": (
            "The diagnostic checks whether transaction creation time has a zero second; "
            "P1.4 mechanism streams now use namespaced minute/second jitter."
        ),
    }
    social_ids = [txn_id for txn_id in social_transaction_ids if txn_id in features_by_id]
    control_ids = [txn_id for txn_id in control_transaction_ids if txn_id in features_by_id]
    contrast_ids = social_ids + control_ids
    contrast_labels = [1] * len(social_ids) + [0] * len(control_ids)
    contrast_aucs = {
        feature: _auc(
            [features_by_id[txn_id][feature] for txn_id in contrast_ids],
            contrast_labels,
        )
        for feature in feature_scores
    }
    contrast_strong = {
        name: auc for name, auc in contrast_aucs.items()
        if auc is not None and (auc >= 0.8 or auc <= 0.2)
    }
    transaction_format_classes_by_truth = {
        label: sorted(
            {
                "sha256"
                if re.fullmatch(r"txn-[0-9a-f]{64}", txn_id)
                else "other"
                for txn_id, outcome in labels_by_id.items()
                if outcome == target
            }
        )
        for label, target in (("fraud", 1), ("legit", 0))
    }
    return {
        "episode_id": episode_id,
        "transaction_count": len(target_labels),
        "fraud_count": sum(target_labels),
        "auc_by_feature": aucs,
        "strong_feature_explanations": {
            name: {"auc": auc, "explanation": explanations[name]}
            for name, auc in strong.items()
        },
        "mechanism_transaction_counts": dict(sorted(mechanism_transaction_counts.items())),
        "fixed_delay_artifacts": fixed_delay_artifacts,
        "by_mechanism": {
            "social_engineering_vs_legit_business_spike": {
                "fraud_transaction_count": len(social_ids),
                "control_transaction_count": len(control_ids),
                "auc_by_feature": contrast_aucs,
                "strong_feature_explanations": {
                    name: {"auc": auc, "explanation": explanations[name]}
                    for name, auc in contrast_strong.items()
                },
            }
        },
        "id_format_artifacts": {
            "transaction_format_classes_by_truth": transaction_format_classes_by_truth,
            "transaction_format_class_auc": aucs["transaction_id_hash_format"],
            "customer_format_class_auc": aucs["customer_id_six_digit_format"],
        },
        "account_age_artifacts": {
            "sender_account_age_days_auc": aucs["sender_account_age_days"],
            "recipient_account_age_days_auc": aucs["recipient_account_age_days"],
            "social_engineering_vs_control_recipient_account_age_days_auc": contrast_aucs[
                "recipient_account_age_days"
            ],
        },
        "scope": "private diagnostic; features were derived from operational transaction history at each transaction timestamp",
    }


def _diagnostic_episode_ids(profile: str, episode_rows: list[dict[str, Any]]) -> tuple[str, ...]:
    """Return the episodes that may carry point-in-time diagnostics for this profile.

    Pilot calibration and test episodes are held out and keep structural counts only.
    The smoke report keeps its round-one contract, so every smoke episode stays listed.
    """
    diagnostic_roles = (
        {"development"} if profile == "pilot" else {"development", "calibration", "test"}
    )
    return ("base", *(row["episode_id"] for row in episode_rows if row["role"] in diagnostic_roles))


def _own_episode_label_states(
    operational: sqlite3.Connection,
    simulator_config: SimulatorConfig,
    *,
    episode_id: str,
    as_of_time: str,
) -> dict[str, int]:
    """Count only the episode's own transactions by label state; base history is excluded."""
    counts = dict.fromkeys(LABEL_STATES, 0)
    for snapshot in snapshot_labels(
        operational,
        simulator_config,
        episode_id=episode_id,
        as_of_time=as_of_time,
    ):
        if snapshot.episode_id == episode_id:
            counts[snapshot.state] += 1
    return counts


def _role_template_support(
    private: sqlite3.Connection,
    episode_rows: list[dict[str, Any]],
    *,
    episode_distributions: dict[str, Any],
    episode_label_states: dict[str, dict[str, int]],
    episode_truth_counts: dict[str, Any],
) -> list[dict[str, Any]]:
    """Count episodes by evaluation role and locked template; structural counts only."""
    templates = {
        row[0]: row[1]
        for row in private.execute("SELECT episode_id, template FROM episode_manifest")
    }
    causes_by_episode: dict[str, Counter[str]] = {}
    for row in private.execute(
        "SELECT episode_id, cause, COUNT(*) FROM incident_truth GROUP BY episode_id, cause"
    ):
        causes_by_episode.setdefault(row[0], Counter())[row[1]] = row[2]
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for episode in episode_rows:
        episode_id = episode["episode_id"]
        if episode_id not in templates:
            raise ValueError(f"private manifest is missing the template for {episode_id!r}")
        group = groups.setdefault(
            (episode["role"], templates[episode_id]),
            {
                "episode_count": 0,
                "transaction_count": 0,
                "label_snapshot_state_counts": dict.fromkeys(LABEL_STATES, 0),
                "truth_positive_count": 0,
                "truth_reported_count": 0,
                "incident_truth_counts_by_cause": Counter(),
            },
        )
        group["episode_count"] += 1
        group["transaction_count"] += episode_distributions[episode_id]["transaction_count"]
        for state, count in episode_label_states.get(episode_id, {}).items():
            group["label_snapshot_state_counts"][state] += count
        truth = episode_truth_counts.get(episode_id)
        if truth is not None:
            group["truth_positive_count"] += truth["truth_positive_count"]
            group["truth_reported_count"] += truth["reported_count"]
        group["incident_truth_counts_by_cause"].update(causes_by_episode.get(episode_id, {}))
    rows: list[dict[str, Any]] = []
    for (role, template), group in sorted(groups.items()):
        states = group["label_snapshot_state_counts"]
        rows.append(
            {
                "role": role,
                "template": template,
                "episode_count": group["episode_count"],
                "transaction_count": group["transaction_count"],
                "label_snapshot_state_counts": states,
                "reported_label_count": states["confirmed_fraud"] + states["confirmed_legit"],
                "truth_positive_count": group["truth_positive_count"],
                "truth_reported_count": group["truth_reported_count"],
                "incident_truth_counts_by_cause": dict(
                    sorted(group["incident_truth_counts_by_cause"].items())
                ),
            }
        )
    return rows


def build_profile_reports(
    operational: sqlite3.Connection,
    private: sqlite3.Connection,
    simulator_config: SimulatorConfig,
    episode_config: EpisodeConfig,
    *,
    profile: str,
    model_config: ModelConfig | None = None,
    model_config_path: str | Path = "configs/model.yaml",
    model_doc_path: str | Path = "docs/model.md",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the public operational and private truth reports for one locked profile."""
    if profile not in REPORT_PROFILES:
        raise ValueError(f"report profile must be one of {REPORT_PROFILES}, got {profile!r}")
    resolved_model = model_config or load_model_config(model_config_path)
    timezone_name = resolved_model.business_timezone
    t0 = _t0_timestamp(simulator_config, timezone_name)
    t0_text = utc_timestamp(t0)
    episode_rows = [
        dict(row)
        for row in operational.execute(
            "SELECT episode_id, role FROM episodes WHERE episode_id <> 'base' ORDER BY episode_id"
        )
    ]
    if not episode_rows:
        raise ValueError(f"{profile} reporting requires at least one registered episode")
    report_episode = episode_rows[0]["episode_id"]
    support_config = resolved_support_config(
        resolved_model, model_config_path, model_doc_path
    )
    support = _audit_support(
        operational,
        simulator_config,
        resolved_model,
        model_config_path,
        model_doc_path,
        report_episode,
    )

    reference_window = support_config["reference_drift"]
    reference_start = date.fromisoformat(reference_window["start_inclusive"])
    reference_end = date.fromisoformat(reference_window["end_exclusive"])
    base_txs = [
        row
        for row in read_as_of(
            operational,
            "transactions",
            episode_id=report_episode,
            t0=t0_text,
            as_of_time=t0_text,
        )
        if row["episode_id"] == "base"
    ]
    base_customers = read_as_of(
        operational,
        "customers",
        episode_id=report_episode,
        t0=t0_text,
        as_of_time=t0_text,
    )
    split_transactions: dict[str, list[dict[str, Any]]] = {}
    for split_name, window in resolved_model.windows():
        split_transactions[split_name] = [
            row
            for row in base_txs
            if window.start_inclusive
            <= _business_date(row["event_at"], timezone_name)
            < window.end_exclusive
        ]
    reference_transactions = [
        row
        for row in base_txs
        if reference_start <= _business_date(row["event_at"], timezone_name) < reference_end
    ]

    public_labels: dict[str, Any] = {}
    private_oracle: dict[str, Any] = {}
    label_policy_time = label_delay_report(
        operational,
        private,
        simulator_config,
        episode_id=report_episode,
        as_of_time=t0_text,
    )
    public_labels["training_snapshot_at_t0"] = {
        "as_of_time": t0_text,
        "label_policy_version": label_policy_time["label_policy_version"],
        "model_version": label_policy_time["model_version"],
        "maturity_horizon_days": label_policy_time["maturity_horizon_days"],
        "observed": label_policy_time["observed"],
    }
    private_oracle["training_snapshot_at_t0"] = {
        "as_of_time": t0_text,
        "oracle": label_policy_time["oracle"],
    }

    episode_distributions: dict[str, Any] = {}
    episode_completeness: dict[str, Any] = {}
    monitoring_episode_support: list[dict[str, Any]] = []
    episode_truth_counts: dict[str, Any] = {}
    episode_label_states: dict[str, dict[str, int]] = {}
    duration_days = episode_config.protocol.duration_days.value
    monitoring_end = t0 + timedelta(days=duration_days)
    for episode in episode_rows:
        episode_id = episode["episode_id"]
        observed_transactions = [
            row
            for row in read_as_of(
                operational,
                "transactions",
                episode_id=episode_id,
                t0=t0_text,
                as_of_time=utc_timestamp(monitoring_end),
            )
            if row["episode_id"] == episode_id
            and t0 <= parse_timestamp(row["event_at"]) < monitoring_end
        ]
        # The shared customer records are queried through the same episode/as-of boundary.
        observed_customers = read_as_of(
            operational,
            "customers",
            episode_id=episode_id,
            t0=t0_text,
            as_of_time=utc_timestamp(monitoring_end),
        )
        episode_distributions[episode_id] = {
            "role": episode["role"],
            **_distribution(observed_transactions, timezone_name),
        }
        episode_completeness[episode_id] = {
            "role": episode["role"],
            **_completeness(observed_transactions, observed_customers, episode_id=episode_id),
        }
        monitoring_episode_support.append(
            {
                "episode_id": episode_id,
                "role": episode["role"],
                "transaction_count": len(observed_transactions),
                "support_status": "available" if observed_transactions else "missing_transactions",
            }
        )
        last_transaction = max(
            (parse_timestamp(row["event_at"]) for row in observed_transactions),
            default=None,
        )
        label_as_of = (
            last_transaction
            + timedelta(
                days=max(
                    simulator_config.label_reporting.maturity_horizon_days.value,
                    simulator_config.label_reporting.maximum_delay_days.value,
                )
            )
            if last_transaction is not None
            else None
        )
        if label_as_of is not None:
            delay_report = label_delay_report(
                operational,
                private,
                simulator_config,
                episode_id=episode_id,
                as_of_time=utc_timestamp(label_as_of),
            )
            public_labels[episode_id] = {
                "as_of_time": utc_timestamp(label_as_of),
                "label_policy_version": delay_report["label_policy_version"],
                "model_version": delay_report["model_version"],
                "maturity_horizon_days": delay_report["maturity_horizon_days"],
                "observed": delay_report["observed"],
            }
            private_oracle[episode_id] = {
                "as_of_time": utc_timestamp(label_as_of),
                "oracle": delay_report["oracle"],
            }
            episode_truth_counts[episode_id] = _truth_counts_for_window(
                private,
                episode_id=episode_id,
                start=simulator_config.timeline.t0_business_date.value,
                end=simulator_config.timeline.t0_business_date.value + timedelta(days=duration_days),
                timezone_name=timezone_name,
            )
            if profile == "pilot":
                episode_label_states[episode_id] = _own_episode_label_states(
                    operational,
                    simulator_config,
                    episode_id=episode_id,
                    as_of_time=utc_timestamp(label_as_of),
                )

    public_cohorts = {
        **support["cohorts"],
        "reference_drift": {
            "date_window": reference_window,
            "transaction_count": len(reference_transactions),
            "support_status": "available" if reference_transactions else "missing_reference",
        },
        "monitoring_test": {
            "date_window": {
                "start_inclusive": simulator_config.timeline.t0_business_date.value.isoformat(),
                "end_exclusive": (
                    simulator_config.timeline.t0_business_date.value
                    + timedelta(days=duration_days)
                ).isoformat(),
            },
            "duration_days": duration_days,
            "episodes": monitoring_episode_support,
            "support_status": (
                "available"
                if all(item["support_status"] == "available" for item in monitoring_episode_support)
                else "missing_transactions"
            ),
        },
    }
    public_report = {
        "report_version": 1,
        "profile": profile,
        "business_timezone": timezone_name,
        "as_of_time": t0_text,
        "label_policy_version": label_policy_time["label_policy_version"],
        "model_version": label_policy_time["model_version"],
        "support_sources": {
            "model_splits": support_config["split_source"],
            "reference_drift": support_config["reference_source"],
        },
        "p2_cohort_support": public_cohorts,
        "operational_distributions": {
            "training_cohorts": {
                name: _distribution(rows, timezone_name)
                for name, rows in split_transactions.items()
            },
            "reference_drift": _distribution(reference_transactions, timezone_name),
            "episodes": episode_distributions,
        },
        "window_completeness": {
            "definition": "max(customer.event_at, customer.available_at) <= transaction.created_at - window",
            "training_cohorts": {
                name: _completeness(rows, base_customers, episode_id=report_episode)
                for name, rows in split_transactions.items()
            },
            "reference_drift": _completeness(
                reference_transactions, base_customers, episode_id=report_episode
            ),
            "monitoring_test_episodes": episode_completeness,
        },
        "label_delay": public_labels,
    }

    # Truth outcomes are keyed by the private P1.5 metadata, not inferred from observed labels.
    private_splits: dict[str, Any] = {}
    for split_name, window in resolved_model.windows():
        private_splits[split_name] = _truth_counts_for_window(
            private,
            episode_id="base",
            start=window.start_inclusive,
            end=window.end_exclusive,
            timezone_name=timezone_name,
        )
    private_splits["reference_drift"] = _truth_counts_for_window(
        private,
        episode_id="base",
        start=reference_start,
        end=reference_end,
        timezone_name=timezone_name,
    )
    incident_counts = {
        row["cause"]: row["count"]
        for row in private.execute(
            "SELECT cause, COUNT(*) AS count FROM incident_truth GROUP BY cause ORDER BY cause"
        )
    }
    p14_diagnostics = {
        episode_id: _p14_feature_diagnostics(
            operational, private, episode_id=episode_id, config=simulator_config
        )
        for episode_id in _diagnostic_episode_ids(profile, episode_rows)
    }
    private_report = {
        "report_version": 1,
        "profile": profile,
        "truth_bearing": True,
        "truth_label_support": {
            "basis": "private_simulator_truth",
            "training_and_reference_cohorts": private_splits,
            "monitoring_test_episodes": episode_truth_counts,
        },
        "label_delay_oracle": private_oracle,
        "incident_truth_counts_by_cause": incident_counts,
        "p1_4_feature_diagnostics": p14_diagnostics,
    }
    if profile == "pilot":
        private_report["role_template_support"] = _role_template_support(
            private,
            episode_rows,
            episode_distributions=episode_distributions,
            episode_label_states=episode_label_states,
            episode_truth_counts=episode_truth_counts,
        )
    return public_report, private_report

