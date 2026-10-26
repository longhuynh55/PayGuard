"""Round-one PSI signals and deterministic operational alerts."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import io
import json
import math
from pathlib import Path
import re
from typing import Any, TextIO

import yaml

from monitoring.metrics import DAILY_METRIC_COLUMNS
from simulator.storage import utc_timestamp


ALERT_COLUMNS = (
    "alert_id",
    "episode_id",
    "as_of_time",
    "metric",
    "feature",
    "segment",
    "value",
    "reference",
    "threshold",
    "event_at",
    "available_at",
)

_CENSOR_FLAG = re.compile(r"^(window|reference)_censored_samples=(\d+)$")
_REFERENCE_NOT_APPLICABLE = "not_applicable"


class MonitoringSignalsError(ValueError):
    """Raised when daily metrics or signal configuration violates P3.2."""


@dataclass(frozen=True)
class _SignalConfig:
    threshold: float
    threshold_provenance: str
    minimum_sample_count: int


@dataclass(frozen=True)
class SignalResult:
    """Alerts and a report of suppressed/ignored metric rows."""

    alerts: tuple[dict[str, Any], ...]
    report: dict[str, Any]


def _parse_as_of(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise MonitoringSignalsError(f"{field} must be a timezone-aware timestamp")
    try:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timezone is missing")
        return utc_timestamp(parsed.astimezone(UTC))
    except (ValueError, TypeError) as exc:
        raise MonitoringSignalsError(
            f"{field} must be a valid timezone-aware timestamp: {value!r}"
        ) from exc


def _load_signal_config(path: str | Path) -> _SignalConfig:
    config_path = Path(path)
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise MonitoringSignalsError(
            f"could not read monitoring config {config_path}: {exc}"
        ) from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise MonitoringSignalsError("monitoring config schema_version must be 1")

    signals = raw.get("signals")
    if not isinstance(signals, dict):
        raise MonitoringSignalsError("monitoring config signals must be an object")
    threshold_config = signals.get("psi_threshold")
    if not isinstance(threshold_config, dict):
        raise MonitoringSignalsError("monitoring signals.psi_threshold must be an object")
    threshold = threshold_config.get("value")
    provenance = threshold_config.get("provenance")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise MonitoringSignalsError("PSI signal threshold must be numeric")
    if not math.isfinite(float(threshold)) or float(threshold) < 0:
        raise MonitoringSignalsError("PSI signal threshold must be finite and non-negative")
    if threshold_config.get("unit") != "PSI":
        raise MonitoringSignalsError("PSI signal threshold unit must be PSI")
    if not isinstance(threshold_config.get("rationale"), str) or not threshold_config[
        "rationale"
    ].strip():
        raise MonitoringSignalsError("PSI signal threshold must state its rationale")
    if not isinstance(provenance, dict) or provenance.get("kind") != "source":
        raise MonitoringSignalsError("PSI signal threshold must declare source provenance")
    provenance_reference = provenance.get("reference")
    if not isinstance(provenance_reference, str) or not provenance_reference.strip():
        raise MonitoringSignalsError("PSI signal threshold must declare its provenance source")

    minimum_sample = raw.get("minimum_sample_count")
    if not isinstance(minimum_sample, dict):
        raise MonitoringSignalsError("monitoring minimum_sample_count must be an object")
    minimum_sample_count = minimum_sample.get("value")
    if (
        isinstance(minimum_sample_count, bool)
        or not isinstance(minimum_sample_count, int)
        or minimum_sample_count < 1
    ):
        raise MonitoringSignalsError("monitoring minimum_sample_count must be a positive integer")

    return _SignalConfig(
        threshold=float(threshold),
        threshold_provenance=provenance_reference,
        minimum_sample_count=minimum_sample_count,
    )


def _validate_metric_row(row: Any, index: int, *, episode_id: str) -> dict[str, Any]:
    if not isinstance(row, dict) or set(row) != set(DAILY_METRIC_COLUMNS):
        raise MonitoringSignalsError(
            f"daily_metrics row {index} does not match the P3.1 output schema"
        )
    if row["episode_id"] != episode_id:
        raise MonitoringSignalsError(
            f"daily_metrics row {index} belongs to episode {row['episode_id']!r}, "
            f"expected {episode_id!r}"
        )
    for field in ("metric_id", "feature", "segment", "reference_window"):
        if not isinstance(row[field], str):
            raise MonitoringSignalsError(f"daily_metrics row {index} {field} must be text")
    if not isinstance(row["flags"], list) or any(
        not isinstance(flag, str) for flag in row["flags"]
    ):
        raise MonitoringSignalsError(f"daily_metrics row {index} flags must be a text list")
    for field in ("n_window", "n_reference"):
        value = row[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise MonitoringSignalsError(
                f"daily_metrics row {index} {field} must be a non-negative integer"
            )
    if row["value"] is not None and (
        isinstance(row["value"], bool)
        or not isinstance(row["value"], (int, float))
    ):
        raise MonitoringSignalsError(
            f"daily_metrics row {index} value must be numeric or null"
        )
    return row


def _alert_id(row: dict[str, Any]) -> str:
    identity = {
        "schema_version": 1,
        "episode_id": row["episode_id"],
        "as_of_time": row["as_of_time"],
        "metric": row["metric_id"],
        "feature": row["feature"],
        "segment": row["segment"],
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return "alert-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def _quality_reasons(row: dict[str, Any], *, minimum_sample_count: int) -> list[str]:
    reasons: list[str] = []
    flags = row["flags"]

    for flag in flags:
        if flag.startswith(("window_censored_samples=", "reference_censored_samples=")):
            matched = _CENSOR_FLAG.fullmatch(flag)
            if matched is None:
                reasons.append("invalid_censoring_metadata")
            elif int(matched.group(2)) > 0:
                reasons.append(flag)

    if "window_incomplete" in flags:
        reasons.append("window_incomplete")
    if "no_window_samples" in flags:
        reasons.append("no_window_samples")
    if "no_reference_samples" in flags:
        reasons.append("no_reference_samples")
    if row["n_window"] < minimum_sample_count and "small_window_sample" not in flags:
        reasons.append("small_window_sample")
    elif "small_window_sample" in flags:
        reasons.append("small_window_sample")
    if row["n_reference"] < minimum_sample_count and "small_reference_sample" not in flags:
        reasons.append("small_reference_sample")
    elif "small_reference_sample" in flags:
        reasons.append("small_reference_sample")

    value = row["value"]
    if value is None:
        reasons.append("missing_value")
    elif not math.isfinite(float(value)):
        reasons.append("non_finite_value")

    return list(dict.fromkeys(reasons))


def compute_alerts(
    daily_metrics: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    episode_id: str,
    as_of_time: str,
    monitoring_config_path: str | Path = "configs/monitoring.yaml",
) -> SignalResult:
    """Create PSI alerts from one episode's P3.1 rows through a requested cutoff.

    Future snapshots are ignored; rows for another episode are rejected so a
    caller cannot accidentally mix parallel episode timelines.
    """
    if not isinstance(episode_id, str) or not episode_id.strip() or episode_id == "base":
        raise MonitoringSignalsError("episode_id must identify a non-base episode")
    if not isinstance(daily_metrics, (list, tuple)):
        raise MonitoringSignalsError("daily_metrics must be P3.1 rows in a list or tuple")
    cutoff = _parse_as_of(as_of_time, field="as_of_time")
    config = _load_signal_config(monitoring_config_path)

    current_rows: list[dict[str, Any]] = []
    future_count = 0
    seen_keys: set[tuple[str, str, str, str]] = set()
    for index, source_row in enumerate(daily_metrics):
        if not isinstance(source_row, dict):
            raise MonitoringSignalsError(f"daily_metrics row {index} must be an object")
        if source_row.get("episode_id") != episode_id:
            raise MonitoringSignalsError(
                f"daily_metrics row {index} belongs to episode "
                f"{source_row.get('episode_id')!r}, expected {episode_id!r}"
            )
        if "as_of_time" not in source_row:
            raise MonitoringSignalsError(f"daily_metrics row {index} is missing as_of_time")
        metric_as_of = _parse_as_of(
            source_row["as_of_time"], field=f"daily_metrics[{index}].as_of_time"
        )
        if metric_as_of > cutoff:
            future_count += 1
            continue
        row = _validate_metric_row(source_row, index, episode_id=episode_id)
        normalized_row = dict(row)
        normalized_row["as_of_time"] = metric_as_of
        row_key = (
            metric_as_of,
            normalized_row["metric_id"],
            normalized_row["feature"],
            normalized_row["segment"],
        )
        if row_key in seen_keys:
            raise MonitoringSignalsError(
                f"duplicate P3.1 daily metric key at row {index}: {row_key!r}"
            )
        seen_keys.add(row_key)
        current_rows.append(normalized_row)

    alerts: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []
    below_threshold_count = 0
    evaluated_count = 0

    for row in current_rows:
        metric = row["metric_id"]
        base_report_row = {
            "episode_id": episode_id,
            "as_of_time": row["as_of_time"],
            "metric": metric,
            "feature": row["feature"],
            "segment": row["segment"],
            "value": row["value"],
            "flags": row["flags"],
        }
        if not metric.startswith("psi.") or row["feature"] == "decision_region":
            reason = (
                "reference_not_available"
                if row["reference_window"] == _REFERENCE_NOT_APPLICABLE
                else "not_a_round_one_psi_metric"
            )
            ignored.append({**base_report_row, "reason": reason})
            continue

        quality_reasons = _quality_reasons(
            row, minimum_sample_count=config.minimum_sample_count
        )
        if row["reference_window"] == _REFERENCE_NOT_APPLICABLE or not row["reference_window"]:
            quality_reasons.append("reference_not_available")
        if quality_reasons:
            suppressed.append(
                {**base_report_row, "reasons": list(dict.fromkeys(quality_reasons))}
            )
            continue

        value = float(row["value"])
        evaluated_count += 1
        if value <= config.threshold:
            below_threshold_count += 1
            continue

        alert = {
            "alert_id": "",
            "episode_id": episode_id,
            "as_of_time": row["as_of_time"],
            "metric": metric,
            "feature": row["feature"],
            "segment": row["segment"],
            "value": value,
            "reference": row["reference_window"],
            "threshold": config.threshold,
            "event_at": row["as_of_time"],
            "available_at": row["as_of_time"],
        }
        alert["alert_id"] = _alert_id(row)
        alerts.append(alert)

    alerts.sort(
        key=lambda row: (
            row["as_of_time"], row["metric"], row["feature"], row["segment"]
        )
    )
    report = {
        "episode_id": episode_id,
        "as_of_time": cutoff,
        "threshold": {
            "value": config.threshold,
            "comparison": ">",
            "unit": "PSI",
            "provenance": config.threshold_provenance,
        },
        "minimum_sample_count": config.minimum_sample_count,
        "daily_metric_rows_received": len(daily_metrics),
        "daily_metric_rows_considered": len(current_rows),
        "future_metric_rows_ignored": future_count,
        "psi_rows_evaluated": evaluated_count,
        "below_or_equal_threshold_count": below_threshold_count,
        "alert_count": len(alerts),
        "suppressed_count": len(suppressed),
        "suppressed": suppressed,
        "ignored_count": len(ignored),
        "ignored": ignored,
    }
    return SignalResult(alerts=tuple(alerts), report=report)


def alerts_csv(rows: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> str:
    """Serialize alerts using the documented operational output schema."""
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=ALERT_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        if set(row) != set(ALERT_COLUMNS):
            raise MonitoringSignalsError("alert row does not match the output schema")
        writer.writerow(
            {
                **row,
                "value": format(float(row["value"]), ".17g"),
                "threshold": format(float(row["threshold"]), ".17g"),
            }
        )
    return stream.getvalue()


def write_alerts_csv(
    rows: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    destination: str | Path | TextIO,
) -> None:
    """Write the alert output as CSV to a path or open text stream."""
    contents = alerts_csv(rows)
    if hasattr(destination, "write"):
        destination.write(contents)
        return
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8", newline="")
