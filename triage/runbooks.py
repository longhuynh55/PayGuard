"""Code-owned checklist for the P4.2 unit or NULL change runbook."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
import math
from pathlib import Path
import sqlite3
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from simulator.asof import read_as_of
from simulator.config import SimulatorConfig, load_simulator_config
from simulator.storage import StorageError, utc_timestamp


_DEFAULT_TRIAGE_CONFIG = Path(__file__).resolve().parents[1] / "configs" / "triage.yaml"
_AMOUNT_FEATURES = frozenset({"amount", "amount_avg_7d"})
_UNAVAILABLE_FRAUD_METRICS = {
    "outgoing_transfers_24h": "No operational P3.5 metric ID is available for outgoing transfers in 24 hours.",
    "first_time_sender": "No operational P3.5 metric ID is available for first-time senders.",
    "amount_near_10m": "No operational P3.5 metric ID is available for amounts near 10 million VND.",
    "model_review_rate": "model.review_rate has no reference baseline and has not emitted a signal.",
}
_CURRENT_NON_FRAUD_METRIC_IDS = frozenset(
    {"psi.txn_count_24h", "psi.amount_avg_7d", "model.review_rate"}
)


class RunbookConfigError(ValueError):
    """Raised when the P4.2 runbook thresholds or fraud allowlist are invalid."""


@dataclass(frozen=True)
class _UnitOrNullPolicy:
    median_ratio_targets: tuple[float, ...]
    median_ratio_multiplicative_band: float
    null_rate_increase: float
    comparison_period_days: int
    minimum_post_change_days: int


@dataclass(frozen=True)
class _FraudMetric:
    key: str
    metric_id: str | None


def load_triage_config(path: str | Path = _DEFAULT_TRIAGE_CONFIG) -> dict[str, Any]:
    """Load the operational triage configuration without exposing private data."""

    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RunbookConfigError(f"could not read triage config: {exc}") from exc
    if not isinstance(raw, dict):
        raise RunbookConfigError("triage config must be an object")
    return raw


def _configured_value(
    raw: Any,
    *,
    name: str,
    provenance_kinds: frozenset[str] = frozenset({"source", "contract"}),
) -> Any:
    if not isinstance(raw, Mapping) or "value" not in raw:
        raise RunbookConfigError(f"{name} must contain a value and provenance")
    provenance = raw.get("provenance")
    if not isinstance(provenance, Mapping):
        raise RunbookConfigError(f"{name} must declare provenance")
    if provenance.get("kind") not in provenance_kinds:
        raise RunbookConfigError(f"{name} has an unsupported provenance kind")
    reference = provenance.get("reference")
    if not isinstance(reference, str) or not reference.strip():
        raise RunbookConfigError(f"{name} must cite its provenance")
    if not isinstance(raw.get("unit"), str) or not raw["unit"].strip():
        raise RunbookConfigError(f"{name} must declare a unit")
    if not isinstance(raw.get("rationale"), str) or not raw["rationale"].strip():
        raise RunbookConfigError(f"{name} must state a rationale")
    return raw["value"]


def _load_unit_or_null_policy(config: Mapping[str, Any]) -> _UnitOrNullPolicy:
    runbooks = config.get("runbooks")
    section = runbooks.get("unit_or_null_change") if isinstance(runbooks, Mapping) else None
    if not isinstance(section, Mapping):
        raise RunbookConfigError("triage config is missing runbooks.unit_or_null_change")

    targets = _configured_value(
        section.get("median_ratio_targets"), name="median_ratio_targets"
    )
    multiplicative_band = _configured_value(
        section.get("median_ratio_multiplicative_band"),
        name="median_ratio_multiplicative_band",
        provenance_kinds=frozenset({"user_decision"}),
    )
    null_increase = _configured_value(
        section.get("null_rate_increase"), name="null_rate_increase"
    )
    comparison_days = _configured_value(
        section.get("comparison_period_days"), name="comparison_period_days"
    )
    post_days = _configured_value(
        section.get("minimum_post_change_days"), name="minimum_post_change_days"
    )

    if not isinstance(targets, list) or not targets:
        raise RunbookConfigError("median_ratio_targets must be a non-empty list")
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0
        for value in targets
    ):
        raise RunbookConfigError("median_ratio_targets must contain positive finite numbers")
    if (
        isinstance(multiplicative_band, bool)
        or not isinstance(multiplicative_band, (int, float))
        or not math.isfinite(float(multiplicative_band))
        or float(multiplicative_band) < 1
    ):
        raise RunbookConfigError(
            "median_ratio_multiplicative_band must be finite and at least one"
        )
    for name, value in (("null_rate_increase", null_increase),):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 0 < float(value) <= 1
        ):
            raise RunbookConfigError(f"{name} must be finite and in (0, 1]")
    if isinstance(comparison_days, bool) or comparison_days != 1:
        raise RunbookConfigError("comparison_period_days is fixed at one day by the runbook")
    if isinstance(post_days, bool) or post_days != 1:
        raise RunbookConfigError("minimum_post_change_days is fixed at one by the runbook")

    return _UnitOrNullPolicy(
        median_ratio_targets=tuple(float(value) for value in targets),
        median_ratio_multiplicative_band=float(multiplicative_band),
        null_rate_increase=float(null_increase),
        comparison_period_days=1,
        minimum_post_change_days=1,
    )


def _load_fraud_allowlist(config: Mapping[str, Any]) -> tuple[_FraudMetric, ...]:
    section = config.get("fraud_side_metrics")
    if not isinstance(section, Mapping):
        raise RunbookConfigError("triage config is missing fraud_side_metrics")
    provenance = section.get("provenance")
    if (
        not isinstance(provenance, Mapping)
        or provenance.get("kind") != "user_decision"
        or not isinstance(provenance.get("reference"), str)
        or not provenance["reference"].strip()
    ):
        raise RunbookConfigError("fraud_side_metrics must cite the user decision")
    rows = section.get("items")
    if rows is None:
        # The YAML mapping stays easy to scan while the code consumes explicit entries.
        rows = [
            {"key": key, "metric_id": metric_id}
            for key, metric_id in section.items()
            if key != "provenance"
        ]
    if not isinstance(rows, list):
        raise RunbookConfigError("fraud_side_metrics entries must be a list")
    expected = set(_UNAVAILABLE_FRAUD_METRICS)
    parsed: list[_FraudMetric] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise RunbookConfigError(f"fraud_side_metrics[{index}] must be an object")
        key = row.get("key")
        metric_id = row.get("metric_id")
        if not isinstance(key, str) or key not in expected:
            raise RunbookConfigError(f"fraud_side_metrics[{index}] has an unsupported key")
        if metric_id is not None and (
            not isinstance(metric_id, str) or not metric_id.strip()
        ):
            raise RunbookConfigError(f"fraud_side_metrics[{index}].metric_id must be text or null")
        parsed.append(_FraudMetric(key=key, metric_id=metric_id))
    if {item.key for item in parsed} != expected or len(parsed) != len(expected):
        raise RunbookConfigError("fraud_side_metrics must list each P4.2 metric exactly once")
    return tuple(parsed)


def _utc_datetime(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a UTC timestamp")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{field} must be a valid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError(f"{field} must be timezone-aware UTC")
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _business_start(day: date, zone: ZoneInfo) -> str:
    return utc_timestamp(datetime.combine(day, time.min, tzinfo=zone))


def _result(
    status: str,
    *,
    reason: str,
    scope_match: bool,
    window_complete: bool,
    details: Mapping[str, Any],
    requires_human_review: bool = False,
) -> dict[str, Any]:
    return {
        "status": status,
        "reason": reason,
        "scope_match": scope_match,
        "window_complete": window_complete,
        "requires_human_review": requires_human_review,
        "details": dict(details),
    }


def _error_checklists(message: str) -> dict[str, dict[str, Any]]:
    detail = {"error": message}
    return {
        "unit_or_null_change": _result(
            "error",
            reason="The runbook could not validate its operational snapshot.",
            scope_match=False,
            window_complete=False,
            details=detail,
            requires_human_review=True,
        ),
        "fraud_signal": _result(
            "error",
            reason="Fraud-side signals could not be checked safely.",
            scope_match=False,
            window_complete=False,
            details=detail,
            requires_human_review=True,
        ),
    }


def _local_date(value: str, zone: ZoneInfo) -> date:
    return _utc_datetime(value, "event_at").astimezone(zone).date()


@dataclass(frozen=True)
class _DailyAmounts:
    business_date: date
    amounts: tuple[int | None, ...]

    @property
    def transaction_count(self) -> int:
        return len(self.amounts)

    @property
    def null_count(self) -> int:
        return sum(amount is None for amount in self.amounts)

    @property
    def non_null_amounts(self) -> tuple[int, ...]:
        return tuple(amount for amount in self.amounts if amount is not None)

    @property
    def median_amount(self) -> float | None:
        values = self.non_null_amounts
        return float(median(values)) if values else None

    @property
    def null_rate(self) -> float:
        return self.null_count / self.transaction_count


def _daily_amounts(
    transactions: list[dict[str, Any]],
    customers: list[dict[str, Any]],
    *,
    episode_id: str,
    segment: str,
    zone: ZoneInfo,
) -> dict[str, dict[date, _DailyAmounts]]:
    customer_segments = {row["customer_id"]: row["segment"] for row in customers}
    grouped: dict[str, dict[date, list[int | None]]] = {}
    for row in transactions:
        if row["episode_id"] != episode_id:
            continue
        if segment != "all":
            customer_segment = customer_segments.get(row["customer_id"])
            requested_segment = segment.removeprefix("segment=")
            if customer_segment is None or customer_segment != requested_segment:
                continue
        amount = row["amount"]
        if amount is not None and (isinstance(amount, bool) or not isinstance(amount, int)):
            raise ValueError("transactions.amount is not an integer or NULL")
        channel = str(row["channel"])
        grouped.setdefault(channel, {}).setdefault(
            _local_date(str(row["event_at"]), zone), []
        ).append(amount)

    return {
        channel: {
            business_date: _DailyAmounts(
                business_date=business_date,
                amounts=tuple(amounts),
            )
            for business_date, amounts in days.items()
        }
        for channel, days in grouped.items()
    }


def _compare_transition(
    before_days: list[date],
    after_days: list[date],
    daily: Mapping[date, _DailyAmounts],
    policy: _UnitOrNullPolicy,
) -> dict[str, Any]:
    before_amounts = [amount for day in before_days for amount in daily[day].amounts]
    after_amounts = [amount for day in after_days for amount in daily[day].amounts]
    before_non_null = [amount for amount in before_amounts if amount is not None]
    after_non_null = [amount for amount in after_amounts if amount is not None]
    before_median = float(median(before_non_null)) if before_non_null else None
    after_median = float(median(after_non_null)) if after_non_null else None
    ratio: float | None = None
    ratio_target: float | None = None
    if (
        before_median is not None
        and after_median is not None
        and before_median > 0
    ):
        ratio = after_median / before_median
        for target in policy.median_ratio_targets:
            lower = target / policy.median_ratio_multiplicative_band
            upper = target * policy.median_ratio_multiplicative_band
            if lower <= ratio <= upper:
                ratio_target = target
                break

    before_null_count = len(before_amounts) - len(before_non_null)
    after_null_count = len(after_amounts) - len(after_non_null)
    before_null_rate = before_null_count / len(before_amounts)
    after_null_rate = after_null_count / len(after_amounts)
    null_rate_change = after_null_rate - before_null_rate
    return {
        "before_start_business_date": min(before_days).isoformat(),
        "before_last_business_date": max(before_days).isoformat(),
        "after_start_business_date": min(after_days).isoformat(),
        "after_last_business_date": max(after_days).isoformat(),
        "before_transaction_count": len(before_amounts),
        "after_transaction_count": len(after_amounts),
        "before_median_amount": before_median,
        "after_median_amount": after_median,
        "median_ratio_after_divided_by_before": ratio,
        "matched_median_ratio_target": ratio_target,
        "before_null_rate": before_null_rate,
        "after_null_rate": after_null_rate,
        "null_rate_increase": null_rate_change,
        "null_rate_threshold": policy.null_rate_increase,
        "null_rate_threshold_met": null_rate_change >= policy.null_rate_increase,
        "abrupt_change_within_one_day": True,
        "transition_boundary_days": policy.comparison_period_days,
    }


def _scope_amount_transactions(
    incident: Mapping[str, Any],
    *,
    as_of_time: str,
    transactions: list[dict[str, Any]],
    customers: list[dict[str, Any]],
    zone: ZoneInfo,
    policy: _UnitOrNullPolicy,
    t0_date: date,
    as_of_local_date: date,
) -> dict[str, Any]:
    feature = incident["feature"]
    segment = incident["segment"]
    if feature not in _AMOUNT_FEATURES:
        return _result(
            "not_applicable",
            reason="The incident feature is outside the amount-change runbook scope.",
            scope_match=False,
            window_complete=True,
            details={
                "as_of_time": as_of_time,
                "incident_feature": feature,
                "supported_features": sorted(_AMOUNT_FEATURES),
                "evaluation_status": "not_applicable",
            },
        )

    daily_by_channel = _daily_amounts(
        transactions,
        customers,
        episode_id=str(incident["episode_id"]),
        segment=segment,
        zone=zone,
    )
    comparisons: list[dict[str, Any]] = []
    observed_days_by_channel: dict[str, list[date]] = {}
    for channel, daily in sorted(daily_by_channel.items()):
        observed_days = sorted(day for day in daily if day <= as_of_local_date)
        observed_days_by_channel[channel] = observed_days
        post_t0_days = [day for day in observed_days if day >= t0_date]
        for after_day in post_t0_days:
            earlier_days = [day for day in observed_days if day < after_day]
            if not earlier_days:
                continue
            immediately_before = max(earlier_days)
            if (after_day - immediately_before).days != policy.comparison_period_days:
                continue
            # Detect the contracted one-day discontinuity using only the
            # adjacent dates; later values must not dilute a persistent shift.
            comparison = _compare_transition(
                [immediately_before], [after_day], daily, policy
            )
            comparison["channel"] = channel
            comparisons.append(comparison)

    triggered = [
        item
        for item in comparisons
        if item["matched_median_ratio_target"] is not None
        or item["null_rate_threshold_met"]
    ]
    if triggered:
        change = triggered[0]
        change_date = date.fromisoformat(change["after_start_business_date"])
        post_change_days_observed = max(0, (as_of_local_date - change_date).days)
        window_complete = post_change_days_observed >= policy.minimum_post_change_days
        checks = {
            "median_ratio": {
                "status": "pass" if change["matched_median_ratio_target"] is not None else "fail",
                "observed_ratio": change["median_ratio_after_divided_by_before"],
                "matched_target": change["matched_median_ratio_target"],
                "targets": list(policy.median_ratio_targets),
                "multiplicative_band": policy.median_ratio_multiplicative_band,
                "accepted_ranges": [
                    {
                        "target": target,
                        "minimum": target / policy.median_ratio_multiplicative_band,
                        "maximum": target * policy.median_ratio_multiplicative_band,
                    }
                    for target in policy.median_ratio_targets
                ],
            },
            "null_rate_increase": {
                "status": "pass" if change["null_rate_threshold_met"] else "fail",
                "observed_increase": change["null_rate_increase"],
                "threshold": policy.null_rate_increase,
            },
            "abrupt_change_within_one_day": {
                "status": "pass",
                "comparison_days": policy.comparison_period_days,
                "before_business_date": change["before_last_business_date"],
                "after_business_date": change["after_start_business_date"],
            },
            "post_change_window": {
                "status": "pass" if window_complete else "insufficient",
                "required_complete_days": policy.minimum_post_change_days,
                "observed_complete_days": post_change_days_observed,
                "change_date": change["after_start_business_date"],
            },
        }
        details = {
            "episode_id": incident["episode_id"],
            "as_of_time": as_of_time,
            "incident_feature": feature,
            "incident_segment": segment,
            "change_detection": checks,
            "matched_transition": change,
            "channel": change["channel"],
            "candidate_transition_count": len(triggered),
            "compared_day_pair_count": len(comparisons),
        }
        if not window_complete:
            details["evaluation_status"] = "insufficient"
            return _result(
                "pending",
                reason="A candidate amount change was found, but one full local business day after the change is not available yet.",
                scope_match=True,
                window_complete=False,
                details=details,
            )
        return _result(
            "pass",
            reason="A contracted median-ratio or NULL-rate change passed over an adjacent business-day boundary.",
            scope_match=True,
            window_complete=True,
            details=details,
        )

    has_adjacent_observations = bool(comparisons)
    if not has_adjacent_observations:
        return _result(
            "pending",
            reason="There are not enough adjacent daily observations to assess a one-day change.",
            scope_match=True,
            window_complete=False,
            details={
                "episode_id": incident["episode_id"],
                "as_of_time": as_of_time,
                "incident_feature": feature,
                "incident_segment": segment,
                "observed_business_dates_by_channel": {
                    channel: [day.isoformat() for day in days]
                    for channel, days in observed_days_by_channel.items()
                },
                "compared_day_pair_count": 0,
                "evaluation_status": "insufficient",
                "change_detection": {
                    "median_ratio": "insufficient",
                    "null_rate_increase": "insufficient",
                    "abrupt_change_within_one_day": "insufficient",
                    "post_change_window": "insufficient",
                },
            },
        )

    return _result(
        "not_applicable",
        reason="No contracted median-ratio or NULL-rate change was observed across adjacent local business days.",
        scope_match=False,
        window_complete=True,
        details={
            "episode_id": incident["episode_id"],
            "as_of_time": as_of_time,
            "incident_feature": feature,
            "incident_segment": segment,
            "compared_day_pair_count": len(comparisons),
            "comparisons": comparisons,
            "change_detection": {
                "median_ratio": {
                    "status": "fail",
                    "targets": list(policy.median_ratio_targets),
                    "multiplicative_band": policy.median_ratio_multiplicative_band,
                    "accepted_ranges": [
                        {
                            "target": target,
                            "minimum": target / policy.median_ratio_multiplicative_band,
                            "maximum": target * policy.median_ratio_multiplicative_band,
                        }
                        for target in policy.median_ratio_targets
                    ],
                },
                "null_rate_increase": "fail",
                "abrupt_change_within_one_day": "pass for evaluated adjacent day pairs",
                "post_change_window": "complete",
            },
        },
    )


def _fraud_signal_check(
    incident: Mapping[str, Any],
    *,
    as_of_time: str,
    metrics: list[dict[str, Any]],
    alerts: list[dict[str, Any]],
    allowlist: tuple[_FraudMetric, ...],
) -> dict[str, Any]:
    incident_segment = incident["segment"]
    evidence: list[dict[str, Any]] = []
    unavailable: list[dict[str, Any]] = []
    other_segment_signals: list[dict[str, Any]] = []

    for item in allowlist:
        base = {"key": item.key, "metric_id": item.metric_id}
        if item.metric_id is None:
            unavailable.append(
                {
                    **base,
                    "status": "unavailable",
                    "reason": _UNAVAILABLE_FRAUD_METRICS[item.key]
                    + " The allowlist metric_id is NULL.",
                }
            )
            continue
        metric_rows = [row for row in metrics if row["metric_id"] == item.metric_id]
        alert_rows = [row for row in alerts if row["metric"] == item.metric_id]
        if not alert_rows:
            reason = _UNAVAILABLE_FRAUD_METRICS[item.key]
            if metric_rows:
                reason += " An operational metric row is present, but no alert was emitted by as_of_time."
            else:
                reason += " No operational metric row or alert is present by as_of_time."
            unavailable.append(
                {
                    **base,
                    "status": "unavailable",
                    "reason": reason,
                    "metric_rows_observed": len(metric_rows),
                    "alerts_observed": 0,
                }
            )
            continue
        for alert in alert_rows:
            source_rows = [
                row
                for row in metric_rows
                if row["as_of_time"] == alert["as_of_time"]
                and row["feature"] == alert["feature"]
                and row["segment"] == alert["segment"]
            ]
            signal = {
                **base,
                "alert_id": alert["alert_id"],
                "feature": alert["feature"],
                "segment": alert["segment"],
                "as_of_time": alert["as_of_time"],
                "source_metric_present": bool(source_rows),
            }
            if not source_rows:
                return _result(
                    "error",
                    reason="An allowlisted fraud alert has no matching as-of operational metric row.",
                    scope_match=False,
                    window_complete=False,
                    requires_human_review=True,
                    details={
                        "episode_id": incident["episode_id"],
                        "as_of_time": as_of_time,
                        "unavailable_metrics": unavailable,
                        "invalid_alert": signal,
                        "inference_limit": "An unavailable metric is not evidence that fraud is absent.",
                    },
                )
            if alert["segment"] == incident_segment:
                evidence.append(signal)
            else:
                other_segment_signals.append(signal)

    details = {
        "episode_id": incident["episode_id"],
        "as_of_time": as_of_time,
        "incident_segment": incident_segment,
        "allowlisted_metrics": [
            {"key": item.key, "metric_id": item.metric_id} for item in allowlist
        ],
        "observed_metric_ids": sorted({str(row["metric_id"]) for row in metrics}),
        "observed_alert_metric_ids": sorted({str(row["metric"]) for row in alerts}),
        "unavailable_metrics": unavailable,
        "other_segment_signals": other_segment_signals,
        "inference_limit": "Unavailable metrics and missing alerts do not count as evidence that fraud is absent.",
    }
    if evidence:
        details["same_segment_signals"] = evidence
        return _result(
            "pass",
            reason="An allowlisted fraud-side alert is available in the same incident segment.",
            scope_match=True,
            window_complete=True,
            details=details,
            requires_human_review=True,
        )
    if other_segment_signals:
        return _result(
            "fail",
            reason="Allowlisted fraud-side alerts are present only in other segments.",
            scope_match=False,
            window_complete=True,
            details=details,
        )
    return _result(
        "not_applicable",
        reason="No allowlisted fraud-side alert is available in this episode and snapshot; unavailable checks are not negative evidence.",
        scope_match=False,
        window_complete=True,
        details=details,
    )


def check_unit_or_null_change(
    operational: sqlite3.Connection,
    incident_snapshot: Mapping[str, Any],
    *,
    as_of_time: str | None = None,
    simulator_config: SimulatorConfig | None = None,
    triage_config: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Return gate-compatible P4.2 checklists from one operational incident snapshot.

    The incident is revalidated through ``read_as_of``. All transaction,
    customer, metric, and alert rows are read through that same API using the
    incident episode and its exact ``available_at`` snapshot cutoff. An
    explicitly supplied cutoff must match the incident snapshot.
    """

    try:
        if not isinstance(incident_snapshot, Mapping):
            raise ValueError("incident snapshot must be a mapping")
        incident = dict(incident_snapshot)
        episode_id = incident.get("episode_id")
        incident_id = incident.get("incident_id")
        as_of_raw = incident.get("available_at")
        if not isinstance(episode_id, str) or not episode_id.strip() or episode_id == "base":
            raise ValueError("incident snapshot has an invalid episode_id")
        if not isinstance(incident_id, str) or not incident_id.strip():
            raise ValueError("incident snapshot has an invalid incident_id")
        incident_as_of = _iso(_utc_datetime(as_of_raw, "incident.available_at"))
        if as_of_time is not None:
            supplied_as_of = _iso(_utc_datetime(as_of_time, "as_of_time"))
            if supplied_as_of != incident_as_of:
                raise ValueError(
                    "as_of_time must equal the incident snapshot available_at"
                )
        as_of = incident_as_of

        sim_config = simulator_config or load_simulator_config()
        config = dict(triage_config) if triage_config is not None else load_triage_config()
        policy = _load_unit_or_null_policy(config)
        allowlist = _load_fraud_allowlist(config)
        try:
            zone = ZoneInfo(sim_config.timeline.business_timezone.value)
        except (ZoneInfoNotFoundError, AttributeError) as exc:
            raise RunbookConfigError("simulator business timezone is invalid") from exc
        t0_date = sim_config.timeline.t0_business_date.value
        t0 = _business_start(t0_date, zone)

        snapshots = read_as_of(
            operational,
            "incidents",
            episode_id=episode_id,
            t0=t0,
            as_of_time=as_of,
        )
        verified = next(
            (
                row
                for row in snapshots
                if row["incident_id"] == incident_id
                and row["event_at"] == incident.get("event_at")
                and row["available_at"] == incident.get("available_at")
            ),
            None,
        )
        if verified is None or verified != incident:
            raise ValueError("incident snapshot is not an operational row available at as_of_time")

        read_kwargs = {"episode_id": episode_id, "t0": t0, "as_of_time": as_of}
        transactions = read_as_of(operational, "transactions", **read_kwargs)
        customers = read_as_of(operational, "customers", **read_kwargs)
        metrics = read_as_of(operational, "daily_metrics", **read_kwargs)
        alerts = read_as_of(operational, "alerts", **read_kwargs)
        as_of_local_date = _utc_datetime(as_of, "as_of_time").astimezone(zone).date()

        return {
            "unit_or_null_change": _scope_amount_transactions(
                verified,
                as_of_time=as_of,
                transactions=transactions,
                customers=customers,
                zone=zone,
                policy=policy,
                t0_date=t0_date,
                as_of_local_date=as_of_local_date,
            ),
            "fraud_signal": _fraud_signal_check(
                verified,
                as_of_time=as_of,
                metrics=metrics,
                alerts=alerts,
                allowlist=allowlist,
            ),
        }
    except (
        AttributeError,
        KeyError,
        RunbookConfigError,
        StorageError,
        TypeError,
        ValueError,
        sqlite3.Error,
    ) as exc:
        return _error_checklists(str(exc))


__all__ = ["RunbookConfigError", "check_unit_or_null_change", "load_triage_config"]
