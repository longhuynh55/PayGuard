"""Incident-to-truth matching; this is the only truth reader."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
import json
from pathlib import Path
import sqlite3
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from model.features import FEATURE_LOOKBACK_DAYS
from simulator.storage import utc_timestamp
from simulator.validate import RUN_METADATA_KEY


# P3 metrics use aggregate feature names, while P1 truth records the changed
# source fields. Keep these aliases explicit; an unknown name only matches an
# identical truth feature and is never guessed from its spelling.
MONITORING_FEATURE_TO_TRUTH_FEATURE = {
    "amount_avg_7d": "amount",
    "txn_count_24h": "txn_count",
    "txn_count": "txn_count",
}


class EvaluationMatcherError(ValueError):
    """Raised when private truth or P3.3 incident rows violate their contract."""


@dataclass(frozen=True)
class PrivateEvaluationData:
    """Truth and episode metadata loaded from the evaluator-only database."""

    episode_ids: tuple[str, ...]
    templates: Mapping[str, str]
    episode_end_at: Mapping[str, str]
    metric_window_days: int
    truth_rows: tuple[dict[str, Any], ...]


def _parse_timestamp(value: Any, *, field: str) -> tuple[datetime, str]:
    if not isinstance(value, str):
        raise EvaluationMatcherError(f"{field} must be a timezone-aware timestamp")
    try:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timezone is missing")
        utc_value = parsed.astimezone(UTC)
    except (TypeError, ValueError) as exc:
        raise EvaluationMatcherError(
            f"{field} must be a valid timezone-aware timestamp: {value!r}"
        ) from exc
    return utc_value, utc_timestamp(utc_value)


def normalize_as_of_time(value: str) -> str:
    """Return a canonical UTC cutoff for filtering available P3.3 snapshots."""
    return _parse_timestamp(value, field="as_of_time")[1]


def _public_metric_window_days() -> int:
    """Read the locked public monitoring window, without caching run state."""
    config_path = Path(__file__).resolve().parents[1] / "configs" / "monitoring.yaml"
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise EvaluationMatcherError(
            f"could not read public monitoring config {config_path}: {exc}"
        ) from exc
    if not isinstance(config, dict) or config.get("schema_version") != 1:
        raise EvaluationMatcherError(
            "public monitoring config schema_version must be 1"
        )
    window = config.get("window_days")
    days = window.get("value") if isinstance(window, dict) else None
    provenance = window.get("provenance") if isinstance(window, dict) else None
    if isinstance(days, bool) or not isinstance(days, int) or days <= 0:
        raise EvaluationMatcherError(
            "public monitoring config window_days.value must be a positive integer"
        )
    if not isinstance(provenance, dict) or provenance.get("kind") != "contract":
        raise EvaluationMatcherError(
            "public monitoring config window_days must be a locked contract value"
        )
    return days


def _public_metric_windows() -> tuple[dict[str, int], dict[str, int]]:
    """Read per-metric windows and resolve them through the public feature map."""
    config_path = Path(__file__).resolve().parents[1] / "configs" / "monitoring.yaml"
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise EvaluationMatcherError(
            f"could not read public monitoring config {config_path}: {exc}"
        ) from exc
    if not isinstance(config, Mapping) or config.get("schema_version") != 1:
        raise EvaluationMatcherError("public monitoring config schema_version must be 1")

    raw_windows = config.get("metric_windows")
    if not isinstance(raw_windows, Mapping) or not raw_windows:
        raise EvaluationMatcherError(
            "public monitoring config metric_windows must be a non-empty object"
        )
    metric_to_feature: dict[str, str] = {}
    raw_features = config.get("features")
    if not isinstance(raw_features, list):
        raise EvaluationMatcherError("public monitoring config features must be an array")
    for index, item in enumerate(raw_features):
        if not isinstance(item, Mapping):
            raise EvaluationMatcherError(
                f"public monitoring config features[{index}] must be an object"
            )
        metric_id, feature = item.get("metric_id"), item.get("feature")
        if not isinstance(metric_id, str) or not metric_id.strip() or not isinstance(
            feature, str
        ) or not feature.strip():
            raise EvaluationMatcherError(
                f"public monitoring config features[{index}] needs metric_id and feature"
            )
        metric_to_feature[metric_id] = feature

    daily_volume = config.get("daily_volume")
    if not isinstance(daily_volume, Mapping):
        raise EvaluationMatcherError("public monitoring config daily_volume must be an object")
    volume_metric_id, volume_feature = (
        daily_volume.get("metric_id"),
        daily_volume.get("feature"),
    )
    if not isinstance(volume_metric_id, str) or not isinstance(volume_feature, str):
        raise EvaluationMatcherError(
            "public monitoring config daily_volume must declare metric_id and feature"
        )
    metric_to_feature[volume_metric_id] = volume_feature

    if set(raw_windows) != set(metric_to_feature):
        raise EvaluationMatcherError(
            "public monitoring config metric_windows must cover every configured metric"
        )
    global_days = _public_metric_window_days()
    windows_by_metric: dict[str, int] = {}
    windows_by_feature: dict[str, int] = {}
    for metric_id, raw_window in raw_windows.items():
        days = raw_window.get("value") if isinstance(raw_window, Mapping) else None
        provenance = raw_window.get("provenance") if isinstance(raw_window, Mapping) else None
        if isinstance(days, bool) or not isinstance(days, int) or days <= 0:
            raise EvaluationMatcherError(
                f"public monitoring config metric_windows.{metric_id}.value must be a positive integer"
            )
        if str(metric_id) == volume_metric_id and days != 1:
            raise EvaluationMatcherError(
                f"monitoring metric_windows {metric_id!r} must be 1 day: daily volume is "
                f"computed for one business date, got {days}"
            )
        unit = raw_window.get("unit") if isinstance(raw_window, Mapping) else None
        rationale = raw_window.get("rationale") if isinstance(raw_window, Mapping) else None
        reference = provenance.get("reference") if isinstance(provenance, Mapping) else None
        if (
            not isinstance(provenance, Mapping)
            or provenance.get("kind") != "contract"
            or not isinstance(reference, str)
            or not reference.strip()
            or not isinstance(unit, str)
            or not unit.strip()
            or not isinstance(rationale, str)
            or not rationale.strip()
        ):
            raise EvaluationMatcherError(
                f"public monitoring config metric_windows.{metric_id} must have locked provenance, unit, and rationale"
            )
        if str(metric_id).startswith("psi.") and days != global_days:
            raise EvaluationMatcherError(
                f"public PSI metric window {metric_id!r} does not match window_days"
            )
        feature = metric_to_feature[str(metric_id)]
        windows_by_metric[str(metric_id)] = days
        prior = windows_by_feature.setdefault(feature, days)
        if prior != days:
            raise EvaluationMatcherError(
                f"public monitoring feature {feature!r} has conflicting metric windows"
            )
    return windows_by_metric, windows_by_feature


def _window_days_from_config(
    config: Mapping[str, Any], *, source: str
) -> int | None:
    """Extract an optional resolved monitoring window from a run config."""
    for key in ("monitoring", "monitoring_config"):
        monitoring_config = config.get(key)
        if isinstance(monitoring_config, Mapping) and "window_days" in monitoring_config:
            break
    else:
        monitoring_config = config
        if "window_days" not in monitoring_config:
            return None
    if not isinstance(monitoring_config, Mapping):
        return None
    window = monitoring_config["window_days"]
    days = window.get("value") if isinstance(window, Mapping) else window
    if isinstance(days, bool) or not isinstance(days, int) or days <= 0:
        raise EvaluationMatcherError(
            f"{source} window_days must be a positive integer"
        )
    return days


def _metric_windows_from_config(
    config: Mapping[str, Any], *, source: str
) -> dict[str, int] | None:
    """Extract optional run-resolved per-metric windows for contract checking."""
    monitoring_config: Any = config
    for key in ("monitoring", "monitoring_config"):
        candidate = config.get(key)
        if isinstance(candidate, Mapping) and "metric_windows" in candidate:
            monitoring_config = candidate
            break
    if not isinstance(monitoring_config, Mapping) or "metric_windows" not in monitoring_config:
        return None
    raw_windows = monitoring_config["metric_windows"]
    if not isinstance(raw_windows, Mapping):
        raise EvaluationMatcherError(f"{source} metric_windows must be an object")
    result: dict[str, int] = {}
    for metric_id, raw_window in raw_windows.items():
        days = raw_window.get("value") if isinstance(raw_window, Mapping) else raw_window
        if isinstance(days, bool) or not isinstance(days, int) or days <= 0:
            raise EvaluationMatcherError(
                f"{source} metric_windows.{metric_id} must be a positive integer"
            )
        result[str(metric_id)] = days
    return result


def _resolved_metric_window_days(
    connection: sqlite3.Connection, manifest_configs: Iterable[str]
) -> int:
    """Use run-resolved settings when present and verify the public contract."""
    public_days = _public_metric_window_days()
    resolved_values: list[tuple[str, int]] = []
    public_metric_windows, _ = _public_metric_windows()
    resolved_metric_windows: list[tuple[str, dict[str, int]]] = []

    for index, config_json in enumerate(manifest_configs):
        try:
            snapshot = json.loads(config_json)
        except json.JSONDecodeError as exc:
            raise EvaluationMatcherError(
                f"episode manifest config {index} is not valid JSON"
            ) from exc
        if isinstance(snapshot, Mapping):
            days = _window_days_from_config(
                snapshot, source=f"episode manifest config {index}"
            )
            if days is not None:
                resolved_values.append((f"episode manifest config {index}", days))
            metric_windows = _metric_windows_from_config(
                snapshot, source=f"episode manifest config {index}"
            )
            if metric_windows is not None:
                resolved_metric_windows.append(
                    (f"episode manifest config {index}", metric_windows)
                )

    try:
        run_metadata = connection.execute(
            "SELECT value_json FROM run_metadata WHERE key = ?",
            (RUN_METADATA_KEY,),
        ).fetchone()
    except sqlite3.Error as exc:
        raise EvaluationMatcherError("could not read private run metadata") from exc
    if run_metadata is not None:
        try:
            metadata = json.loads(run_metadata["value_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise EvaluationMatcherError(
                f"run metadata {RUN_METADATA_KEY!r} is not valid JSON"
            ) from exc
        if isinstance(metadata, Mapping):
            resolved = metadata.get("resolved_config")
            if isinstance(resolved, Mapping):
                days = _window_days_from_config(
                    resolved,
                    source=f"run metadata {RUN_METADATA_KEY!r} resolved_config",
                )
                if days is not None:
                    resolved_values.append(
                        (
                            f"run metadata {RUN_METADATA_KEY!r} resolved_config",
                            days,
                        )
                    )
                metric_windows = _metric_windows_from_config(
                    resolved,
                    source=f"run metadata {RUN_METADATA_KEY!r} resolved_config",
                )
                if metric_windows is not None:
                    resolved_metric_windows.append(
                        (
                            f"run metadata {RUN_METADATA_KEY!r} resolved_config",
                            metric_windows,
                        )
                    )

    distinct_values = {days for _, days in resolved_values}
    if len(distinct_values) > 1:
        details = ", ".join(f"{source}={days}" for source, days in resolved_values)
        raise EvaluationMatcherError(
            f"run monitoring window configs disagree: {details}"
        )
    if distinct_values:
        resolved_days = next(iter(distinct_values))
        if resolved_days != public_days:
            raise EvaluationMatcherError(
                "run-resolved monitoring window does not match the locked public "
                f"contract ({resolved_days} != {public_days} days)"
            )
    else:
        resolved_days = public_days
    distinct_metric_windows = {
        tuple(sorted(metric_windows.items()))
        for _, metric_windows in resolved_metric_windows
    }
    if len(distinct_metric_windows) > 1:
        details = "; ".join(
            f"{source}={dict(sorted(metric_windows.items()))}"
            for source, metric_windows in resolved_metric_windows
        )
        raise EvaluationMatcherError(
            f"run per-metric window configs disagree: {details}"
        )
    if distinct_metric_windows:
        resolved_metric_windows_map = dict(next(iter(distinct_metric_windows)))
        if resolved_metric_windows_map != public_metric_windows:
            raise EvaluationMatcherError(
                "run-resolved per-metric windows do not match the locked public contract "
                f"({resolved_metric_windows_map!r} != {public_metric_windows!r})"
            )
    return public_days


def _episode_end_from_config(config_json: str, *, episode_id: str) -> str:
    try:
        snapshot = json.loads(config_json)
        simulator_timeline = snapshot["simulator_config"]["timeline"]
        episode_protocol = snapshot["episode_config"]["protocol"]
        t0_date = date.fromisoformat(simulator_timeline["t0_business_date"]["value"])
        timezone_name = simulator_timeline["business_timezone"]["value"]
        duration_days = episode_protocol["duration_days"]["value"]
        timezone = ZoneInfo(timezone_name)
    except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise EvaluationMatcherError(
            f"episode manifest config is incomplete for {episode_id!r}"
        ) from exc
    if isinstance(duration_days, bool) or not isinstance(duration_days, int) or duration_days <= 0:
        raise EvaluationMatcherError(
            f"episode duration must be a positive integer for {episode_id!r}"
        )
    end_local = datetime.combine(t0_date, time.min, tzinfo=timezone) + timedelta(
        days=duration_days
    )
    return utc_timestamp(end_local.astimezone(UTC))


def load_private_evaluation_data(
    private_db_path: str | Path, *, episode_ids: Iterable[str]
) -> PrivateEvaluationData:
    """Load the selected locked manifest and incident truth from a private DB.

    No operational database is opened here. ``episode_ids`` is explicit so a
    truth row with no alert remains in the recall denominator.
    """
    requested_ids = tuple(episode_ids)
    if any(
        not isinstance(episode_id, str)
        or not episode_id.strip()
        or episode_id == "base"
        for episode_id in requested_ids
    ):
        raise EvaluationMatcherError("episode_ids must contain non-base episode IDs")
    ids = tuple(sorted(set(requested_ids)))
    if not ids:
        raise EvaluationMatcherError("episode_ids must contain non-base episode IDs")

    database_path = Path(private_db_path).expanduser().resolve()
    if not database_path.is_file():
        raise EvaluationMatcherError(f"private evaluator database does not exist: {database_path}")

    connection = sqlite3.connect(f"{database_path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    placeholders = ", ".join("?" for _ in ids)
    try:
        manifests = connection.execute(
            f"SELECT episode_id, template, config_json FROM episode_manifest "
            f"WHERE episode_id IN ({placeholders}) ORDER BY episode_id",
            ids,
        ).fetchall()
        if {row["episode_id"] for row in manifests} != set(ids):
            found = {row["episode_id"] for row in manifests}
            missing = ", ".join(sorted(set(ids) - found))
            raise EvaluationMatcherError(
                f"episode IDs are not present in the private manifest: {missing}"
            )

        templates: dict[str, str] = {}
        episode_ends: dict[str, str] = {}
        for row in manifests:
            episode_id = row["episode_id"]
            template = row["template"]
            if not isinstance(template, str) or not template:
                raise EvaluationMatcherError(
                    f"episode template is invalid for {episode_id!r}"
                )
            templates[episode_id] = template
            episode_ends[episode_id] = _episode_end_from_config(
                row["config_json"], episode_id=episode_id
            )

        metric_window_days = _resolved_metric_window_days(
            connection, (row["config_json"] for row in manifests)
        )

        truth_rows = connection.execute(
            f"SELECT * FROM incident_truth WHERE episode_id IN ({placeholders}) "
            "ORDER BY episode_id, incident_truth_id",
            ids,
        ).fetchall()
    except sqlite3.Error as exc:
        raise EvaluationMatcherError(
            f"could not read private incident truth and episode manifest: {exc}"
        ) from exc
    finally:
        connection.close()

    normalized_truth: list[dict[str, Any]] = []
    for source in truth_rows:
        row = dict(source)
        if row["requires_intervention"] not in (0, 1):
            raise EvaluationMatcherError(
                f"requires_intervention is invalid for {row['incident_truth_id']!r}"
            )
        row["template"] = templates[row["episode_id"]]
        parent_scenario = row.get("parent_scenario")
        if parent_scenario is not None and (
            not isinstance(parent_scenario, str) or not parent_scenario.strip()
        ):
            raise EvaluationMatcherError(
                f"parent_scenario must be NULL or non-empty text for {row['incident_truth_id']!r}"
            )
        normalized_truth.append(row)

    return PrivateEvaluationData(
        episode_ids=ids,
        templates=templates,
        episode_end_at=episode_ends,
        metric_window_days=metric_window_days,
        truth_rows=tuple(normalized_truth),
    )


def assert_operational_roles_match_private_manifest(
    private_db_path: str | Path, operational_roles: Mapping[str, str]
) -> None:
    """Refuse operational role labels that disagree with the private episode manifest.

    Every episode in ``operational_roles`` must exist in the private
    ``episode_manifest`` with the same role. Callers run this before any truth
    read, report write, or role-ledger touch, so a relabeled operational row
    cannot steer the evaluator toward held-out truth.
    """
    if not operational_roles:
        return
    database_path = Path(private_db_path).expanduser().resolve()
    if not database_path.is_file():
        raise EvaluationMatcherError(f"private evaluator database does not exist: {database_path}")
    episode_ids = tuple(sorted(str(episode_id) for episode_id in operational_roles))
    placeholders = ", ".join("?" for _ in episode_ids)
    try:
        connection = sqlite3.connect(f"{database_path.as_uri()}?mode=ro", uri=True)
        try:
            rows = connection.execute(
                "SELECT episode_id, role FROM episode_manifest "
                f"WHERE episode_id IN ({placeholders})",
                episode_ids,
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise EvaluationMatcherError(
            f"could not read private episode_manifest roles: {exc}"
        ) from exc
    private_roles = {str(row[0]): str(row[1]) for row in rows}
    differences: list[str] = []
    for episode_id in episode_ids:
        operational_role = str(operational_roles[episode_id])
        private_role = private_roles.get(episode_id)
        if private_role is None:
            differences.append(f"{episode_id}: operational={operational_role}, private=missing")
        elif private_role != operational_role:
            differences.append(
                f"{episode_id}: operational={operational_role}, private={private_role}"
            )
    if differences:
        raise EvaluationMatcherError(
            "operational episode roles disagree with the private episode_manifest.role: "
            + "; ".join(differences)
        )


def _json_text_array(value: Any, *, field: str, truth_id: str) -> tuple[str, ...]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise EvaluationMatcherError(
                f"{field} must be valid JSON for truth {truth_id!r}"
            ) from exc
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise EvaluationMatcherError(
            f"{field} must be an array of non-empty strings for truth {truth_id!r}"
        )
    return tuple(value)


def _snapshot_alert_ids(value: Any, *, incident_id: str) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise EvaluationMatcherError(
            f"incident {incident_id!r} alert_ids_json must be JSON text"
        )
    try:
        alert_ids = json.loads(value)
    except json.JSONDecodeError as exc:
        raise EvaluationMatcherError(
            f"incident {incident_id!r} alert_ids_json must be valid JSON"
        ) from exc
    if (
        not isinstance(alert_ids, list)
        or not alert_ids
        or any(not isinstance(alert_id, str) or not alert_id.strip() for alert_id in alert_ids)
        or len(alert_ids) != len(set(alert_ids))
    ):
        raise EvaluationMatcherError(
            f"incident {incident_id!r} alert_ids_json must be a non-empty unique ID array"
        )
    return tuple(alert_ids)


def _truth_window(
    truth: Mapping[str, Any],
    *,
    episode_end_at: str,
    metric_window_days: int,
    feature_lookback_days: int,
) -> tuple[datetime, datetime]:
    truth_id = truth.get("incident_truth_id")
    effect_start, _ = _parse_timestamp(
        truth.get("effect_start_at"), field=f"truth {truth_id!r} effect_start_at"
    )
    effect_available, _ = _parse_timestamp(
        truth.get("effect_available_at"),
        field=f"truth {truth_id!r} effect_available_at",
    )
    start = max(effect_start, effect_available)
    raw_end = truth.get("effect_end_at")
    if raw_end is None:
        end, _ = _parse_timestamp(
            episode_end_at, field=f"episode {truth.get('episode_id')!r} end"
        )
    else:
        end, _ = _parse_timestamp(
            raw_end, field=f"truth {truth_id!r} effect_end_at"
        )
    if end < start:
        raise EvaluationMatcherError(
            f"truth effect interval ends before it can become available: {truth_id!r}"
        )
    return start, end + timedelta(
        days=metric_window_days + feature_lookback_days
    )


def _segment_matches(incident_segment: str, affected_segments: tuple[str, ...]) -> bool:
    # P3.1's `all` denotes an aggregate over every segment, so it overlaps each
    # listed truth segment. Specific monitoring segments require an exact match.
    return incident_segment in affected_segments or (
        incident_segment == "all" and bool(affected_segments)
    )


def _scope_matches(
    incident: Mapping[str, Any],
    truth: Mapping[str, Any],
    *,
    truth_window: tuple[datetime, datetime],
    incident_window: tuple[datetime, datetime],
) -> bool:
    truth_id = str(truth["incident_truth_id"])
    incident_feature = incident["feature"]
    if not isinstance(incident_feature, str) or not incident_feature.strip():
        return False
    truth_feature = MONITORING_FEATURE_TO_TRUTH_FEATURE.get(
        incident_feature, incident_feature
    )
    affected_features = _json_text_array(
        truth["affected_features_json"],
        field="affected_features_json",
        truth_id=truth_id,
    )
    affected_segments = _json_text_array(
        truth["affected_segments_json"],
        field="affected_segments_json",
        truth_id=truth_id,
    )
    feature_matches = truth_feature in affected_features
    segment_matches = _segment_matches(incident["segment"], affected_segments)
    incident_start, incident_end = incident_window
    truth_start, truth_end = truth_window
    time_matches = incident_start <= truth_end and truth_start <= incident_end
    return feature_matches and segment_matches and time_matches


def _shares_parent_scenario(parents: Mapping[str, str | None]) -> bool:
    """True when every candidate carries the same non-NULL parent_scenario.

    User decision 31/10/2026 (docs/evaluation.md, section 2): such candidates form
    one matched incident with every component truth in ``matched_truth_ids``.
    """
    for truth_id, parent_scenario in parents.items():
        if parent_scenario is not None and (
            not isinstance(parent_scenario, str) or not parent_scenario.strip()
        ):
            raise EvaluationMatcherError(
                "parent_scenario must be NULL or non-empty text for truth "
                f"{truth_id!r}"
            )
    values = set(parents.values())
    return len(values) == 1 and None not in values


def match_incidents(
    incidents: Iterable[Mapping[str, Any]],
    truth_rows: Iterable[Mapping[str, Any]],
    *,
    episode_ids: Iterable[str],
    episode_end_at: Mapping[str, str],
    as_of_time: str,
    metric_window_days: int,
) -> tuple[dict[str, Any], ...]:
    """Match P3.3 incident snapshots to selected private truths.

    The truth interval includes each metric's public window and the monitored
    feature's own lookback. Candidates that all share one non-NULL
    ``parent_scenario`` are matched together; other multi-candidate incidents stay
    ``ambiguous_match``. Every candidate remains available for risk and routing
    metrics, and multiple incidents may independently match one truth.
    """
    selected_ids = set(episode_ids)
    _, as_of = _parse_timestamp(as_of_time, field="as_of_time")
    as_of_dt, _ = _parse_timestamp(as_of, field="as_of_time")
    if (
        isinstance(metric_window_days, bool)
        or not isinstance(metric_window_days, int)
        or metric_window_days <= 0
    ):
        raise EvaluationMatcherError("metric_window_days must be a positive integer")
    _, metric_windows_by_feature = _public_metric_windows()
    public_window_days = _public_metric_window_days()
    if metric_window_days != public_window_days:
        raise EvaluationMatcherError(
            "metric_window_days does not match the locked public monitoring contract "
            f"({metric_window_days} != {public_window_days})"
        )

    truth_by_episode: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    seen_truth_ids: set[str] = set()
    for index, truth in enumerate(truth_rows):
        truth_id = truth.get("incident_truth_id")
        episode_id = truth.get("episode_id")
        if not isinstance(truth_id, str) or not truth_id.strip():
            raise EvaluationMatcherError(f"truth row {index} incident_truth_id is invalid")
        if truth_id in seen_truth_ids:
            raise EvaluationMatcherError(f"duplicate incident_truth_id: {truth_id!r}")
        if episode_id not in selected_ids:
            raise EvaluationMatcherError(
                f"truth {truth_id!r} is outside the selected episode set"
            )
        seen_truth_ids.add(truth_id)
        truth_by_episode[episode_id].append(truth)

    snapshots_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    identities: dict[str, tuple[str, str, str, str]] = {}
    for index, source in enumerate(incidents):
        if not isinstance(source, Mapping):
            raise EvaluationMatcherError(f"incident row {index} must be an object")
        row = dict(source)
        incident_id = row.get("incident_id")
        episode_id = row.get("episode_id")
        feature = row.get("feature")
        segment = row.get("segment")
        if not isinstance(incident_id, str) or not incident_id.strip():
            raise EvaluationMatcherError(f"incident row {index} incident_id is invalid")
        if episode_id not in selected_ids:
            raise EvaluationMatcherError(
                f"incident {incident_id!r} is outside the selected episode set"
            )
        if not isinstance(feature, str) or not feature.strip():
            raise EvaluationMatcherError(f"incident {incident_id!r} feature is invalid")
        if not isinstance(segment, str) or not segment.strip():
            raise EvaluationMatcherError(f"incident {incident_id!r} segment is invalid")
        opened_at, normalized_opened = _parse_timestamp(
            row.get("opened_at"), field=f"incident {incident_id!r} opened_at"
        )
        event_at, normalized_event = _parse_timestamp(
            row.get("event_at"), field=f"incident {incident_id!r} event_at"
        )
        available_at, normalized_available = _parse_timestamp(
            row.get("available_at"), field=f"incident {incident_id!r} available_at"
        )
        if event_at < opened_at:
            raise EvaluationMatcherError(
                f"incident {incident_id!r} event_at predates opened_at"
            )
        if available_at < event_at:
            raise EvaluationMatcherError(
                f"incident {incident_id!r} is available before its event_at"
            )
        if available_at > as_of_dt:
            continue

        identity = (episode_id, feature, segment, normalized_opened)
        prior_identity = identities.setdefault(incident_id, identity)
        if prior_identity != identity:
            raise EvaluationMatcherError(
                f"incident snapshots disagree on identity fields: {incident_id!r}"
            )
        row["opened_at"] = normalized_opened
        row["event_at"] = normalized_event
        row["available_at"] = normalized_available
        snapshots_by_id[incident_id].append(row)

    matches: list[dict[str, Any]] = []
    for incident_id in sorted(snapshots_by_id):
        rows = sorted(
            snapshots_by_id[incident_id],
            key=lambda row: (row["event_at"], row["available_at"]),
        )
        first = rows[0]
        previous_alert_ids: tuple[str, ...] = ()
        alert_available_times: list[datetime] = []
        for row in rows:
            current_alert_ids = _snapshot_alert_ids(
                row.get("alert_ids_json"), incident_id=incident_id
            )
            if current_alert_ids[: len(previous_alert_ids)] != previous_alert_ids:
                raise EvaluationMatcherError(
                    f"incident {incident_id!r} alert_ids_json is not append-only"
                )
            if len(current_alert_ids) > len(previous_alert_ids):
                # P3.3 snapshots preserve cumulative alert IDs. A new ID means
                # this snapshot contains an actual alert event; a decision-only
                # snapshot leaves the list unchanged and cannot extend recall.
                alert_available_times.append(
                    _parse_timestamp(
                        row["available_at"], field=f"incident {incident_id!r} alert available_at"
                    )[0]
                )
            previous_alert_ids = current_alert_ids
        if not alert_available_times:
            raise EvaluationMatcherError(
                f"incident {incident_id!r} has no visible alert snapshot"
            )
        incident_window = (min(alert_available_times), max(alert_available_times))
        monitoring_feature = first["feature"]
        try:
            feature_lookback_days = FEATURE_LOOKBACK_DAYS[monitoring_feature]
        except KeyError as exc:
            raise EvaluationMatcherError(
                f"monitoring feature {monitoring_feature!r} has no public lookback"
            ) from exc
        try:
            feature_metric_window_days = metric_windows_by_feature[monitoring_feature]
        except KeyError as exc:
            raise EvaluationMatcherError(
                f"monitoring feature {monitoring_feature!r} has no public metric window"
            ) from exc
        candidate_parents: dict[str, str | None] = {}
        for truth in sorted(
            truth_by_episode.get(first["episode_id"], ()),
            key=lambda item: str(item["incident_truth_id"]),
        ):
            truth_window = _truth_window(
                truth,
                episode_end_at=episode_end_at[first["episode_id"]],
                metric_window_days=feature_metric_window_days,
                feature_lookback_days=feature_lookback_days,
            )
            if _scope_matches(
                first,
                truth,
                truth_window=truth_window,
                incident_window=incident_window,
            ):
                candidate_parents[str(truth["incident_truth_id"])] = truth.get(
                    "parent_scenario"
                )
        candidate_truth_ids = sorted(candidate_parents)
        if not candidate_truth_ids:
            status = "unmatched"
        elif len(candidate_truth_ids) == 1 or _shares_parent_scenario(candidate_parents):
            status = "matched"
        else:
            status = "ambiguous_match"
        matches.append(
            {
                "incident_id": incident_id,
                "episode_id": first["episode_id"],
                "feature": first["feature"],
                "segment": first["segment"],
                "incident_start_at": utc_timestamp(incident_window[0]),
                "incident_end_at": utc_timestamp(incident_window[1]),
                "status": status,
                "matched_truth_ids": candidate_truth_ids if status == "matched" else [],
                "candidate_truth_ids": candidate_truth_ids,
            }
        )
    return tuple(matches)
