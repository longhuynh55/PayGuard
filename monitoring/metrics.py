"""Daily point-in-time PSI and model review-rate metrics."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
import io
import json
import math
from pathlib import Path
import sqlite3
from typing import Any, TextIO
from zoneinfo import ZoneInfo

import numpy as np
import yaml

from model.features import ScoreFeatureMatrix, build_score_feature_matrix
from simulator.asof import read_as_of
from simulator.config import EpisodeConfig, SimulatorConfig, load_episode_config, load_simulator_config
from simulator.storage import utc_timestamp


DAILY_METRIC_COLUMNS = (
    "metric_id",
    "episode_id",
    "as_of_time",
    "business_date",
    "feature",
    "segment",
    "value",
    "reference_window",
    "window_start",
    "window_end",
    "n_window",
    "n_reference",
    "flags",
)

_FEATURE_OBSERVATION_FLAG = {
    "txn_count_24h": "history_window_observed_24h",
    "amount_avg_7d": "history_window_observed_7d",
}
_SUPPORTED_FEATURES = frozenset(_FEATURE_OBSERVATION_FLAG)
_REFERENCE_NOT_APPLICABLE = "not_applicable"


class MonitoringMetricsError(ValueError):
    """Raised when monitoring input or configuration violates the P3.1 contract."""


@dataclass(frozen=True)
class _FeatureSpec:
    metric_id: str
    feature: str


@dataclass(frozen=True)
class _MonitoringConfig:
    reference_start: date
    reference_end_exclusive: date
    reference_as_of_date: date
    window_days: int
    minimum_sample_count: int
    psi_bins: int
    psi_epsilon: float
    features: tuple[_FeatureSpec, ...]
    review_rate_metric_id: str
    review_rate_feature: str


@dataclass(frozen=True)
class _FeatureSamples:
    values: tuple[float, ...]
    censored_count: int


def _as_date(value: Any, *, field: str) -> date:
    if not isinstance(value, str):
        raise MonitoringMetricsError(f"monitoring config {field} must be an ISO date string")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise MonitoringMetricsError(f"monitoring config {field} must be an ISO date") from exc


def _config_value(raw: Any, *, field: str, expected_provenance: str | None = None) -> Any:
    if not isinstance(raw, dict) or "value" not in raw:
        raise MonitoringMetricsError(f"monitoring config {field} must contain a value")
    provenance = raw.get("provenance")
    if not isinstance(provenance, dict) or not isinstance(provenance.get("reference"), str):
        raise MonitoringMetricsError(f"monitoring config {field} must declare provenance")
    kind = provenance.get("kind")
    if kind not in {"contract", "user_decision", "source", "assumption"}:
        raise MonitoringMetricsError(f"monitoring config {field} has invalid provenance kind")
    if expected_provenance is not None and kind != expected_provenance:
        raise MonitoringMetricsError(
            f"monitoring config {field} provenance must be {expected_provenance!r}"
        )
    if not str(raw.get("unit", "")).strip() or not str(raw.get("rationale", "")).strip():
        raise MonitoringMetricsError(f"monitoring config {field} must state unit and rationale")
    if kind == "assumption" and "not tuned" not in provenance["reference"].lower():
        raise MonitoringMetricsError(f"monitoring config {field} must record that it is not tuned")
    return raw["value"]


def _load_monitoring_config(path: str | Path) -> _MonitoringConfig:
    config_path = Path(path)
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise MonitoringMetricsError(f"could not read monitoring config {config_path}: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise MonitoringMetricsError("monitoring config schema_version must be 1")

    reference = raw.get("reference")
    if not isinstance(reference, dict):
        raise MonitoringMetricsError("monitoring config reference must be an object")
    reference_start = _as_date(
        _config_value(reference.get("start_business_date"), field="reference.start_business_date"),
        field="reference.start_business_date",
    )
    reference_end = _as_date(
        _config_value(
            reference.get("end_business_date_exclusive"),
            field="reference.end_business_date_exclusive",
        ),
        field="reference.end_business_date_exclusive",
    )
    reference_as_of = _as_date(
        _config_value(
            reference.get("as_of_business_date"), field="reference.as_of_business_date"
        ),
        field="reference.as_of_business_date",
    )
    if reference_start >= reference_end:
        raise MonitoringMetricsError("monitoring reference date range must be non-empty")

    window_days = _config_value(raw.get("window_days"), field="window_days")
    if isinstance(window_days, bool) or window_days != 7:
        raise MonitoringMetricsError("P3.1 window_days is fixed at 7")
    minimum_sample_count = _config_value(
        raw.get("minimum_sample_count"),
        field="minimum_sample_count",
        expected_provenance="assumption",
    )
    if isinstance(minimum_sample_count, bool) or not isinstance(minimum_sample_count, int):
        raise MonitoringMetricsError("monitoring minimum_sample_count must be an integer")
    if minimum_sample_count < 1:
        raise MonitoringMetricsError("monitoring minimum_sample_count must be positive")

    psi = raw.get("psi")
    if not isinstance(psi, dict):
        raise MonitoringMetricsError("monitoring config psi must be an object")
    psi_bins = _config_value(
        psi.get("number_of_bins"), field="psi.number_of_bins", expected_provenance="assumption"
    )
    psi_epsilon = _config_value(
        psi.get("epsilon"), field="psi.epsilon", expected_provenance="assumption"
    )
    if isinstance(psi_bins, bool) or not isinstance(psi_bins, int) or psi_bins < 2:
        raise MonitoringMetricsError("monitoring PSI number_of_bins must be an integer >= 2")
    if isinstance(psi_epsilon, bool) or not isinstance(psi_epsilon, (int, float)):
        raise MonitoringMetricsError("monitoring PSI epsilon must be numeric")
    if not math.isfinite(float(psi_epsilon)) or float(psi_epsilon) <= 0:
        raise MonitoringMetricsError("monitoring PSI epsilon must be finite and positive")

    raw_features = raw.get("features")
    if not isinstance(raw_features, list) or not raw_features:
        raise MonitoringMetricsError("monitoring config features must be a non-empty list")
    features: list[_FeatureSpec] = []
    for index, item in enumerate(raw_features):
        if not isinstance(item, dict):
            raise MonitoringMetricsError(f"monitoring config features[{index}] must be an object")
        metric_id = item.get("metric_id")
        feature = item.get("feature")
        if not isinstance(metric_id, str) or not metric_id.strip():
            raise MonitoringMetricsError(f"monitoring config features[{index}] needs metric_id")
        if feature not in _SUPPORTED_FEATURES:
            raise MonitoringMetricsError(
                f"monitoring config features[{index}] needs a P2.2 feature with an observation flag; "
                f"got {feature!r}"
            )
        features.append(_FeatureSpec(metric_id=metric_id, feature=feature))
    if len({item.metric_id for item in features}) != len(features):
        raise MonitoringMetricsError("monitoring metric_id values must be unique")

    review_rate = raw.get("model_review_rate")
    if not isinstance(review_rate, dict):
        raise MonitoringMetricsError("monitoring config model_review_rate must be an object")
    review_metric_id = review_rate.get("metric_id")
    review_feature = review_rate.get("feature")
    if not isinstance(review_metric_id, str) or not review_metric_id.strip():
        raise MonitoringMetricsError("monitoring model_review_rate needs a metric_id")
    if review_feature != "decision_region":
        raise MonitoringMetricsError("monitoring review-rate feature must be decision_region")
    if review_metric_id in {item.metric_id for item in features}:
        raise MonitoringMetricsError("monitoring metric_id values must be unique")

    return _MonitoringConfig(
        reference_start=reference_start,
        reference_end_exclusive=reference_end,
        reference_as_of_date=reference_as_of,
        window_days=window_days,
        minimum_sample_count=minimum_sample_count,
        psi_bins=psi_bins,
        psi_epsilon=float(psi_epsilon),
        features=tuple(features),
        review_rate_metric_id=review_metric_id,
        review_rate_feature=review_feature,
    )


def _parse_timestamp(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MonitoringMetricsError(f"timestamp must include a timezone: {value!r}")
    return parsed.astimezone(UTC)


def _local_date(value: str, timezone: ZoneInfo) -> date:
    return _parse_timestamp(value).astimezone(timezone).date()


def _daily_as_of(business_date: date, timezone: ZoneInfo) -> str:
    local_end = datetime.combine(business_date, time.max, tzinfo=timezone)
    return utc_timestamp(local_end.astimezone(UTC))


def _start_of_business_date(business_date: date, timezone: ZoneInfo) -> datetime:
    return datetime.combine(business_date, time.min, tzinfo=timezone)


def _registered_episodes(
    operational: sqlite3.Connection, selected_episode_ids: tuple[str, ...] | None
) -> tuple[str, ...]:
    registered = tuple(
        str(row[0])
        for row in operational.execute(
            "SELECT episode_id FROM episodes WHERE episode_id <> 'base' ORDER BY episode_id"
        )
    )
    if not registered:
        raise MonitoringMetricsError("monitoring requires at least one registered episode")
    if selected_episode_ids is None:
        return registered
    if not selected_episode_ids:
        raise MonitoringMetricsError("episode_ids must not be empty")
    if len(set(selected_episode_ids)) != len(selected_episode_ids):
        raise MonitoringMetricsError("episode_ids must not contain duplicates")
    unknown = set(selected_episode_ids) - set(registered)
    if unknown:
        raise MonitoringMetricsError(
            f"episode_ids are not registered: {', '.join(sorted(unknown))}"
        )
    return tuple(sorted(selected_episode_ids))


def _feature_rows_by_txn_id(
    matrix: ScoreFeatureMatrix,
) -> dict[str, dict[str, int | float | None]]:
    return {
        example.txn_id: dict(zip(matrix.feature_names, values, strict=True))
        for example, values in zip(matrix.examples, matrix.values, strict=True)
    }


def _feature_samples(
    txn_ids: list[str],
    rows_by_txn_id: dict[str, dict[str, int | float | None]],
    feature: str,
) -> _FeatureSamples:
    observation_flag = _FEATURE_OBSERVATION_FLAG[feature]
    values: list[float] = []
    censored_count = 0
    for txn_id in txn_ids:
        row = rows_by_txn_id.get(txn_id)
        if row is None:
            continue
        if row[observation_flag] != 1:
            censored_count += 1
            continue
        value = row[feature]
        if value is not None and math.isfinite(float(value)):
            values.append(float(value))
    return _FeatureSamples(tuple(values), censored_count)


def _quantile_edges(reference: np.ndarray, bins: int) -> np.ndarray:
    if np.all(reference == reference[0]):
        center = float(reference[0])
        delta = max(abs(center) * 1e-6, 1e-6)
        return np.linspace(center - delta, center + delta, bins + 1, dtype=np.float64)[1:-1]
    quantiles = np.quantile(reference, np.arange(1, bins, dtype=np.float64) / bins)
    return np.unique(np.asarray(quantiles, dtype=np.float64))


def population_stability_index(
    window_values: tuple[float, ...] | list[float],
    reference_values: tuple[float, ...] | list[float],
    *,
    bins: int,
    epsilon: float,
) -> float | None:
    """Return PSI using reference quantiles and epsilon-smoothed proportions."""
    if isinstance(bins, bool) or not isinstance(bins, int) or bins < 2:
        raise MonitoringMetricsError("PSI bins must be an integer >= 2")
    if not math.isfinite(epsilon) or epsilon <= 0:
        raise MonitoringMetricsError("PSI epsilon must be finite and positive")
    reference = np.asarray(reference_values, dtype=np.float64)
    window = np.asarray(window_values, dtype=np.float64)
    reference = reference[np.isfinite(reference)]
    window = window[np.isfinite(window)]
    if not len(reference) or not len(window):
        return None

    internal_edges = _quantile_edges(reference, bins)
    reference_counts = np.histogram(reference, bins=np.r_[-np.inf, internal_edges, np.inf])[0]
    window_counts = np.histogram(window, bins=np.r_[-np.inf, internal_edges, np.inf])[0]
    bin_count = len(reference_counts)
    reference_proportions = (reference_counts + epsilon) / (
        len(reference) + epsilon * bin_count
    )
    window_proportions = (window_counts + epsilon) / (len(window) + epsilon * bin_count)
    value = float(
        np.sum(
            (window_proportions - reference_proportions)
            * np.log(window_proportions / reference_proportions)
        )
    )
    return 0.0 if abs(value) < 1e-15 else value


def _base_metric(
    *,
    metric_id: str,
    episode_id: str,
    as_of_time: str,
    business_date: date,
    feature: str,
    segment: str,
    value: float | None,
    reference_window: str,
    window_start: date,
    n_window: int,
    n_reference: int,
    flags: list[str],
) -> dict[str, Any]:
    return {
        "metric_id": metric_id,
        "episode_id": episode_id,
        "as_of_time": as_of_time,
        "business_date": business_date.isoformat(),
        "feature": feature,
        "segment": segment,
        "value": value,
        "reference_window": reference_window,
        "window_start": window_start.isoformat(),
        "window_end": business_date.isoformat(),
        "n_window": n_window,
        "n_reference": n_reference,
        "flags": flags,
    }


def compute_daily_metrics(
    operational: sqlite3.Connection,
    *,
    simulator_config: SimulatorConfig | None = None,
    episode_config: EpisodeConfig | None = None,
    simulator_config_path: str | Path = "configs/simulator.yaml",
    episode_config_path: str | Path = "configs/episodes.yaml",
    monitoring_config_path: str | Path = "configs/monitoring.yaml",
    episode_ids: tuple[str, ...] | list[str] | None = None,
) -> list[dict[str, Any]]:
    """Build the 30-day daily metric table without reading outside ``read_as_of``.

    PSI uses the fixed base reference. The model review rate is a raw window
    proportion because P2.5 stores ``model_scores`` for post-T0 episodes only.
    """
    monitoring_config = _load_monitoring_config(monitoring_config_path)
    simulator_config = simulator_config or load_simulator_config(simulator_config_path)
    episode_config = episode_config or load_episode_config(episode_config_path)
    timezone = ZoneInfo(simulator_config.timeline.business_timezone.value)
    t0_date = simulator_config.timeline.t0_business_date.value
    if monitoring_config.reference_as_of_date != t0_date:
        raise MonitoringMetricsError(
            "monitoring reference as_of_business_date must match simulator T0"
        )
    if monitoring_config.reference_start < simulator_config.timeline.start_business_date.value:
        raise MonitoringMetricsError("monitoring reference starts before configured base history")
    if monitoring_config.reference_end_exclusive > t0_date:
        raise MonitoringMetricsError("monitoring reference must end no later than T0")

    t0_time = _start_of_business_date(t0_date, timezone)
    t0_utc = utc_timestamp(t0_time.astimezone(UTC))
    reference_as_of = utc_timestamp(
        _start_of_business_date(monitoring_config.reference_as_of_date, timezone).astimezone(UTC)
    )
    reference_window = (
        f"{monitoring_config.reference_start.isoformat()}/"
        f"{(monitoring_config.reference_end_exclusive - timedelta(days=1)).isoformat()}"
    )
    selected_ids = tuple(episode_ids) if episode_ids is not None else None
    registered_episodes = _registered_episodes(operational, selected_ids)

    metric_rows: list[dict[str, Any]] = []
    for episode_id in registered_episodes:
        reference_snapshot = read_as_of(
            operational,
            "transactions",
            episode_id=episode_id,
            t0=t0_utc,
            as_of_time=reference_as_of,
        )
        reference_sources = [row for row in reference_snapshot if row["episode_id"] == "base"]
        reference_targets = [
            row
            for row in reference_sources
            if monitoring_config.reference_start
            <= _local_date(str(row["event_at"]), timezone)
            < monitoring_config.reference_end_exclusive
        ]
        reference_targets_unavailable = [
            row
            for row in reference_targets
            if _parse_timestamp(str(row["available_at"]))
            > _parse_timestamp(str(row["created_at"]))
        ]
        available_reference_targets = [
            row for row in reference_targets if row not in reference_targets_unavailable
        ]
        reference_features = build_score_feature_matrix(
            operational,
            available_reference_targets,
            episode_id=episode_id,
            t0=t0_utc,
            include_base_targets=True,
        )
        reference_feature_rows = _feature_rows_by_txn_id(reference_features)
        reference_target_ids = [str(row["txn_id"]) for row in available_reference_targets]

        last_business_date = t0_date + timedelta(
            days=episode_config.protocol.duration_days.value - 1
        )
        last_as_of = _daily_as_of(last_business_date, timezone)
        episode_snapshot = read_as_of(
            operational,
            "transactions",
            episode_id=episode_id,
            t0=t0_utc,
            as_of_time=last_as_of,
        )
        episode_targets = [
            row
            for row in episode_snapshot
            if row["episode_id"] == episode_id
            and t0_date <= _local_date(str(row["event_at"]), timezone) <= last_business_date
        ]
        episode_targets_unavailable = {
            str(row["txn_id"])
            for row in episode_targets
            if _parse_timestamp(str(row["available_at"]))
            > _parse_timestamp(str(row["created_at"]))
        }
        available_episode_targets = [
            row
            for row in episode_targets
            if str(row["txn_id"]) not in episode_targets_unavailable
        ]
        episode_features = build_score_feature_matrix(
            operational,
            available_episode_targets,
            episode_id=episode_id,
            t0=t0_utc,
        )
        episode_feature_rows = _feature_rows_by_txn_id(episode_features)

        for day_offset in range(episode_config.protocol.duration_days.value):
            business_date = t0_date + timedelta(days=day_offset)
            as_of_time = _daily_as_of(business_date, timezone)
            window_start = business_date - timedelta(days=monitoring_config.window_days - 1)
            complete_window = window_start >= t0_date

            transaction_snapshot = read_as_of(
                operational,
                "transactions",
                episode_id=episode_id,
                t0=t0_utc,
                as_of_time=as_of_time,
            )
            episode_sources = [
                row
                for row in transaction_snapshot
                if row["episode_id"] in {"base", episode_id}
            ]
            window_targets = [
                row
                for row in episode_sources
                if row["episode_id"] == episode_id
                and window_start <= _local_date(str(row["event_at"]), timezone) <= business_date
            ]
            window_target_ids = [str(row["txn_id"]) for row in window_targets]
            unavailable_target_count = sum(
                txn_id in episode_targets_unavailable for txn_id in window_target_ids
            )

            for spec in monitoring_config.features:
                window_samples = _feature_samples(
                    window_target_ids, episode_feature_rows, spec.feature
                )
                reference_samples = _feature_samples(
                    reference_target_ids, reference_feature_rows, spec.feature
                )
                window_values = window_samples.values
                baseline_values = reference_samples.values
                flags: list[str] = []
                if not complete_window:
                    flags.append("window_incomplete")
                if len(window_values) < monitoring_config.minimum_sample_count:
                    flags.append("small_window_sample")
                if len(baseline_values) < monitoring_config.minimum_sample_count:
                    flags.append("small_reference_sample")
                if not window_values:
                    flags.append("no_window_samples")
                if not baseline_values:
                    flags.append("no_reference_samples")
                if window_samples.censored_count:
                    flags.append(f"window_censored_samples={window_samples.censored_count}")
                if reference_samples.censored_count:
                    flags.append(
                        f"reference_censored_samples={reference_samples.censored_count}"
                    )
                if unavailable_target_count:
                    flags.append(f"target_unavailable_at_decision={unavailable_target_count}")
                if reference_targets_unavailable:
                    flags.append(
                        "reference_targets_unavailable_at_decision="
                        f"{len(reference_targets_unavailable)}"
                    )
                value = population_stability_index(
                    window_values,
                    baseline_values,
                    bins=monitoring_config.psi_bins,
                    epsilon=monitoring_config.psi_epsilon,
                )
                metric_rows.append(
                    _base_metric(
                        metric_id=spec.metric_id,
                        episode_id=episode_id,
                        as_of_time=as_of_time,
                        business_date=business_date,
                        feature=spec.feature,
                        segment="all",
                        value=value,
                        reference_window=reference_window,
                        window_start=window_start,
                        n_window=len(window_values),
                        n_reference=len(baseline_values),
                        flags=flags,
                    )
                )

            score_snapshot = read_as_of(
                operational,
                "model_scores",
                episode_id=episode_id,
                t0=t0_utc,
                as_of_time=as_of_time,
            )
            window_scores = [
                row
                for row in score_snapshot
                if row["episode_id"] == episode_id
                and window_start <= _local_date(str(row["event_at"]), timezone) <= business_date
            ]
            model_versions = sorted({str(row["model_version"]) for row in window_scores})
            if not model_versions:
                model_versions = [""]
            for model_version in model_versions:
                model_scores = [
                    row
                    for row in window_scores
                    if str(row["model_version"]) == model_version
                ]
                score_count = len(model_scores)
                review_count = sum(row["decision_region"] == "review" for row in model_scores)
                flags = []
                if not complete_window:
                    flags.append("window_incomplete")
                if score_count < monitoring_config.minimum_sample_count:
                    flags.append("small_window_sample")
                if score_count == 0:
                    flags.append("no_window_samples")
                segment = f"model_version={model_version}" if model_version else "all"
                metric_rows.append(
                    _base_metric(
                        metric_id=monitoring_config.review_rate_metric_id,
                        episode_id=episode_id,
                        as_of_time=as_of_time,
                        business_date=business_date,
                        feature=monitoring_config.review_rate_feature,
                        segment=segment,
                        value=(review_count / score_count if score_count else None),
                        reference_window=_REFERENCE_NOT_APPLICABLE,
                        window_start=window_start,
                        n_window=score_count,
                        n_reference=0,
                        flags=flags,
                    )
                )

    return metric_rows


def metrics_csv(rows: list[dict[str, Any]]) -> str:
    """Serialize the daily metric table using its documented column order."""
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=DAILY_METRIC_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        if set(row) != set(DAILY_METRIC_COLUMNS):
            raise MonitoringMetricsError("daily metric row does not match the output schema")
        writer.writerow(
            {
                **row,
                "value": "" if row["value"] is None else format(float(row["value"]), ".17g"),
                "flags": json.dumps(row["flags"], ensure_ascii=False, separators=(",", ":")),
            }
        )
    return stream.getvalue()


def write_metrics_csv(rows: list[dict[str, Any]], destination: str | Path | TextIO) -> None:
    """Write the daily metric table as CSV to a path or open text stream."""
    contents = metrics_csv(rows)
    if hasattr(destination, "write"):
        destination.write(contents)
        return
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8", newline="")
