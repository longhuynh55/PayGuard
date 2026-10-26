"""Build smoke support reports from operational and private simulator stores."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from model.config import load_pipeline_config
from model.data import InsufficientSplitSupportError, ModelConfig, build_splits, load_model_config
from simulator.asof import read_as_of
from simulator.config import EpisodeConfig, SimulatorConfig
from simulator.labels import label_delay_report, label_truth_metadata_key
from simulator.storage import utc_timestamp


REFERENCE_ROW_RE = re.compile(
    r"^\|\s*Reference drift PSI/KS[^|]*\|\s*\[(\d{2}/\d{2}/\d{4}),\s*"
    r"(\d{2}/\d{2}/\d{4})\)\s*\|$",
    re.MULTILINE,
)
WINDOWS = (("1h", timedelta(hours=1)), ("24h", timedelta(hours=24)), ("7d", timedelta(days=7)))


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _t0_timestamp(config: SimulatorConfig, timezone_name: str) -> datetime:
    return datetime.combine(
        config.timeline.t0_business_date.value,
        time.min,
        tzinfo=ZoneInfo(timezone_name),
    ).astimezone(UTC)


def _business_date(value: str, timezone_name: str) -> date:
    return _parse_timestamp(value).astimezone(ZoneInfo(timezone_name)).date()


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
                missing.append(f"calibration_support_{required}_per_class")
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
        f"{_parse_timestamp(row['event_at']).astimezone(ZoneInfo(timezone_name)).hour:02d}"
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
            created_at = _parse_timestamp(transaction["created_at"])
            if customer is None or customer["episode_id"] not in {"base", episode_id}:
                continue
            if _parse_timestamp(customer["available_at"]) > created_at:
                continue
            observed_from = max(
                _parse_timestamp(customer["event_at"]),
                _parse_timestamp(customer["available_at"]),
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


def build_smoke_reports(
    operational: sqlite3.Connection,
    private: sqlite3.Connection,
    simulator_config: SimulatorConfig,
    episode_config: EpisodeConfig,
    *,
    model_config: ModelConfig | None = None,
    model_config_path: str | Path = "configs/model.yaml",
    model_doc_path: str | Path = "docs/model.md",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a public operational report and a separate private truth report."""
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
        raise ValueError("smoke reporting requires at least one registered episode")
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
            and t0 <= _parse_timestamp(row["event_at"]) < monitoring_end
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
            (_parse_timestamp(row["event_at"]) for row in observed_transactions),
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
        "profile": "smoke",
        "business_timezone": timezone_name,
        "as_of_time": t0_text,
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
    private_report = {
        "report_version": 1,
        "profile": "smoke",
        "truth_bearing": True,
        "truth_label_support": {
            "basis": "private_simulator_truth",
            "training_and_reference_cohorts": private_splits,
            "monitoring_test_episodes": episode_truth_counts,
        },
        "label_delay_oracle": private_oracle,
        "incident_truth_counts_by_cause": incident_counts,
    }
    return public_report, private_report

