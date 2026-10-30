"""Incident-to-truth matching; this is the only truth reader."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
import json
from pathlib import Path
import sqlite3
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from simulator.storage import utc_timestamp


# P3 metrics use aggregate feature names, while P1 truth records the changed
# source fields. Keep these aliases explicit; an unknown name only matches an
# identical truth feature and is never guessed from its spelling.
MONITORING_FEATURE_TO_TRUTH_FEATURE = {
    "amount_avg_7d": "amount",
    "txn_count_24h": "txn_count",
}


class EvaluationMatcherError(ValueError):
    """Raised when private truth or P3.3 incident rows violate their contract."""


@dataclass(frozen=True)
class PrivateEvaluationData:
    """Truth and episode metadata loaded from the evaluator-only database."""

    episode_ids: tuple[str, ...]
    templates: Mapping[str, str]
    episode_end_at: Mapping[str, str]
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


@lru_cache(maxsize=1)
def configured_metric_window_days() -> int:
    """Read the public monitoring window; truth metadata cannot set this value."""
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
        normalized_truth.append(row)

    return PrivateEvaluationData(
        episode_ids=ids,
        templates=templates,
        episode_end_at=episode_ends,
        truth_rows=tuple(normalized_truth),
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
    truth: Mapping[str, Any], *, episode_end_at: str, metric_window_days: int
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
    return start, end + timedelta(days=metric_window_days)


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


def match_incidents(
    incidents: Iterable[Mapping[str, Any]],
    truth_rows: Iterable[Mapping[str, Any]],
    *,
    episode_ids: Iterable[str],
    episode_end_at: Mapping[str, str],
    as_of_time: str,
) -> tuple[dict[str, Any], ...]:
    """Match P3.3 incident snapshots to selected private truths.

    The truth interval includes the delayed effect of the public monitoring
    window. An incident compatible with multiple truth components is ambiguous;
    multiple incidents may independently match the same truth component.
    """
    selected_ids = set(episode_ids)
    _, as_of = _parse_timestamp(as_of_time, field="as_of_time")
    as_of_dt, _ = _parse_timestamp(as_of, field="as_of_time")
    metric_window_days = configured_metric_window_days()

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
        matched_truth_ids: list[str] = []
        for truth in sorted(
            truth_by_episode.get(first["episode_id"], ()),
            key=lambda item: str(item["incident_truth_id"]),
        ):
            truth_window = _truth_window(
                truth,
                episode_end_at=episode_end_at[first["episode_id"]],
                metric_window_days=metric_window_days,
            )
            if _scope_matches(
                first,
                truth,
                truth_window=truth_window,
                incident_window=incident_window,
            ):
                matched_truth_ids.append(str(truth["incident_truth_id"]))
        candidate_truth_ids = sorted(matched_truth_ids)
        if not candidate_truth_ids:
            status = "unmatched"
        elif len(candidate_truth_ids) == 1:
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
