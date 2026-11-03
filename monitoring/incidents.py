"""Deterministic alert-to-incident grouping for one episode."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import sqlite3
from typing import Any, Protocol, TextIO
from zoneinfo import ZoneInfo

from monitoring.signals import ALERT_COLUMNS
from simulator.asof import read_as_of
from simulator.common import TimestampParseError, parse_timestamp
from simulator.storage import utc_timestamp


INCIDENT_COLUMNS = (
    "incident_id",
    "parent_incident_id",
    "episode_id",
    "alert_ids_json",
    "feature",
    "segment",
    "opened_at",
    "status",
    "event_at",
    "available_at",
)

FINAL_ACTIONS = frozenset({"close", "data_ticket", "fraud_ops", "human_review"})
DECISION_ACTIONS = FINAL_ACTIONS | {"recheck"}
_MERGE_WINDOW = timedelta(days=2)


class MonitoringIncidentsError(ValueError):
    """Raised when alert or decision input violates the incident contract."""


class DecisionSource(Protocol):
    """P4-facing source of immutable decisions for an episode."""

    def decisions_for_episode(self, episode_id: str) -> Iterable[Mapping[str, Any]]:
        """Return decisions known to the source for ``episode_id``."""


@dataclass(frozen=True)
class InMemoryDecisionSource:
    """Small adapter for tests and the P3 CLI's optional JSON decision input."""

    decisions: tuple[Mapping[str, Any], ...] = ()

    def __init__(self, decisions: Iterable[Mapping[str, Any]] = ()) -> None:
        object.__setattr__(self, "decisions", tuple(dict(row) for row in decisions))

    def decisions_for_episode(self, episode_id: str) -> tuple[Mapping[str, Any], ...]:
        return tuple(
            row
            for row in self.decisions
            if row.get("episode_id") in (None, episode_id)
        )


@dataclass(frozen=True)
class OperationalDecisionSource:
    """Read P4 decisions as-of and apply them at the next Vietnam daily snapshot.

    The monitoring job for a business day runs before triage writes its decision.
    A decision at the day's cutoff therefore changes the incident at the next
    daily cutoff, while remaining immutable in the operational decisions table.
    """

    operational: sqlite3.Connection
    t0: str
    as_of_time: str

    def decisions_for_episode(self, episode_id: str) -> tuple[dict[str, Any], ...]:
        cutoff, normalized_cutoff = _parse_timestamp(
            self.as_of_time, field_name="decision source as_of_time"
        )
        visible = read_as_of(
            self.operational,
            "decisions",
            episode_id=episode_id,
            t0=self.t0,
            as_of_time=normalized_cutoff,
        )
        rows: list[dict[str, Any]] = []
        for source_row in visible:
            row = dict(source_row)
            effective_at = _next_business_snapshot(row["available_at"])
            effective_dt, _ = _parse_timestamp(
                effective_at, field_name="decision effective_at"
            )
            if effective_dt <= cutoff:
                # The monitoring reducer consumes the effective incident event time.
                # The stored decision_at remains unchanged in the operational table.
                row["decision_at"] = effective_at
                rows.append(row)
        return tuple(rows)


def _next_business_snapshot(value: str) -> str:
    available_at, _ = _parse_timestamp(value, field_name="decision available_at")
    zone = ZoneInfo("Asia/Ho_Chi_Minh")
    local = available_at.astimezone(zone)
    snapshot_date = local.date()
    if local.timetz().replace(tzinfo=None) >= time.max:
        snapshot_date += timedelta(days=1)
    next_snapshot = datetime.combine(snapshot_date, time.max, tzinfo=zone)
    return utc_timestamp(next_snapshot)


@dataclass(frozen=True)
class IncidentResult:
    """Versioned incident snapshots and a truth-free summary."""

    incidents: tuple[dict[str, Any], ...]
    report: dict[str, Any]


@dataclass
class _Alert:
    row: dict[str, Any]
    as_of: datetime


@dataclass
class _Decision:
    row: dict[str, Any]
    decision_at: datetime


@dataclass
class _IncidentState:
    incident_id: str
    parent_incident_id: str | None
    episode_id: str
    feature: str
    segment: str
    opened_at: str
    opened_at_dt: datetime
    alert_ids: list[str] = field(default_factory=list)
    latest_alert_at: datetime | None = None
    status: str = "open"
    final_decision_at: datetime | None = None
    final_decision_id: str | None = None
    event_at: str = ""
    available_at: str = ""


def _parse_timestamp(value: Any, *, field_name: str) -> tuple[datetime, str]:
    try:
        utc_value = parse_timestamp(value)
    except TimestampParseError as exc:
        if exc.reason == "not_text":
            raise MonitoringIncidentsError(
                f"{field_name} must be a timezone-aware timestamp"
            ) from None
        raise MonitoringIncidentsError(
            f"{field_name} must be a valid timezone-aware timestamp: {value!r}"
        ) from exc
    return utc_value, utc_timestamp(utc_value)


def _validate_episode_id(episode_id: Any) -> str:
    if not isinstance(episode_id, str) or not episode_id.strip() or episode_id == "base":
        raise MonitoringIncidentsError("episode_id must identify a non-base episode")
    return episode_id


def _validate_alerts(
    alerts: list[dict[str, Any]] | tuple[dict[str, Any], ...], *, episode_id: str
) -> list[_Alert]:
    if not isinstance(alerts, (list, tuple)):
        raise MonitoringIncidentsError("alerts must be P3.2 rows in a list or tuple")

    validated: list[_Alert] = []
    seen_ids: set[str] = set()
    for index, source_row in enumerate(alerts):
        if not isinstance(source_row, dict) or set(source_row) != set(ALERT_COLUMNS):
            raise MonitoringIncidentsError(
                f"alert row {index} does not match the P3.2 output schema"
            )
        row = dict(source_row)
        if row["episode_id"] != episode_id:
            raise MonitoringIncidentsError(
                f"alert row {index} belongs to episode {row['episode_id']!r}, "
                f"expected {episode_id!r}"
            )
        alert_id = row["alert_id"]
        if not isinstance(alert_id, str) or not alert_id.strip():
            raise MonitoringIncidentsError(f"alert row {index} alert_id must be non-empty text")
        if alert_id in seen_ids:
            raise MonitoringIncidentsError(f"duplicate alert_id: {alert_id!r}")
        seen_ids.add(alert_id)

        for field_name in ("metric", "feature", "segment", "reference"):
            if not isinstance(row[field_name], str) or not row[field_name].strip():
                raise MonitoringIncidentsError(
                    f"alert row {index} {field_name} must be non-empty text"
                )
        for field_name in ("value", "threshold"):
            value = row[field_name]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise MonitoringIncidentsError(
                    f"alert row {index} {field_name} must be numeric"
                )
            if not math.isfinite(float(value)):
                raise MonitoringIncidentsError(
                    f"alert row {index} {field_name} must be finite"
                )

        as_of, normalized_as_of = _parse_timestamp(
            row["as_of_time"], field_name=f"alert row {index} as_of_time"
        )
        event_at, normalized_event_at = _parse_timestamp(
            row["event_at"], field_name=f"alert row {index} event_at"
        )
        available_at, normalized_available_at = _parse_timestamp(
            row["available_at"], field_name=f"alert row {index} available_at"
        )
        if normalized_event_at != normalized_as_of:
            raise MonitoringIncidentsError(
                f"alert row {index} event_at must equal as_of_time"
            )
        if available_at > as_of:
            raise MonitoringIncidentsError(
                f"alert row {index} is unavailable at its as_of_time"
            )
        row["as_of_time"] = normalized_as_of
        row["event_at"] = normalized_event_at
        row["available_at"] = normalized_available_at
        validated.append(_Alert(row=row, as_of=event_at))

    validated.sort(
        key=lambda alert: (
            alert.as_of,
            alert.row["alert_id"],
            alert.row["feature"],
            alert.row["segment"],
        )
    )
    return validated


def _load_decisions(
    decision_source: DecisionSource | None, *, episode_id: str
) -> list[_Decision]:
    source = InMemoryDecisionSource() if decision_source is None else decision_source
    getter = getattr(source, "decisions_for_episode", None)
    if not callable(getter):
        raise MonitoringIncidentsError(
            "decision_source must implement decisions_for_episode(episode_id)"
        )
    try:
        source_rows = getter(episode_id)
        rows = list(source_rows)
    except Exception as exc:
        if isinstance(exc, MonitoringIncidentsError):
            raise
        raise MonitoringIncidentsError(f"could not read episode decisions: {exc}") from exc

    validated: list[_Decision] = []
    seen_ids: set[str] = set()
    for index, source_row in enumerate(rows):
        if not isinstance(source_row, Mapping):
            raise MonitoringIncidentsError(f"decision row {index} must be an object")
        row = dict(source_row)
        decision_id = row.get("decision_id")
        incident_id = row.get("incident_id")
        action = row.get("action")
        if not isinstance(decision_id, str) or not decision_id.strip():
            raise MonitoringIncidentsError(
                f"decision row {index} decision_id must be non-empty text"
            )
        if decision_id in seen_ids:
            raise MonitoringIncidentsError(f"duplicate decision_id: {decision_id!r}")
        seen_ids.add(decision_id)
        if not isinstance(incident_id, str) or not incident_id.strip():
            raise MonitoringIncidentsError(
                f"decision row {index} incident_id must be non-empty text"
            )
        if not isinstance(action, str) or action not in DECISION_ACTIONS:
            raise MonitoringIncidentsError(
                f"decision row {index} action must be one of "
                f"{', '.join(sorted(DECISION_ACTIONS))}"
            )
        if "episode_id" in row and row["episode_id"] != episode_id:
            raise MonitoringIncidentsError(
                f"decision row {index} belongs to episode {row['episode_id']!r}, "
                f"expected {episode_id!r}"
            )
        decision_at, normalized_decision_at = _parse_timestamp(
            row.get("decision_at"), field_name=f"decision row {index} decision_at"
        )
        row["decision_at"] = normalized_decision_at
        validated.append(_Decision(row=row, decision_at=decision_at))

    validated.sort(
        key=lambda decision: (
            decision.decision_at,
            decision.row["decision_id"],
            decision.row["incident_id"],
        )
    )
    return validated


def _incident_id(
    *,
    episode_id: str,
    first_alert: dict[str, Any],
    parent_incident_id: str | None,
) -> str:
    identity = {
        "schema_version": 1,
        "episode_id": episode_id,
        "first_alert_id": first_alert["alert_id"],
        "feature": first_alert["feature"],
        "segment": first_alert["segment"],
        "parent_incident_id": parent_incident_id,
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return "incident-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def _snapshot(state: _IncidentState) -> dict[str, Any]:
    return {
        "incident_id": state.incident_id,
        "parent_incident_id": state.parent_incident_id,
        "episode_id": state.episode_id,
        "alert_ids_json": json.dumps(
            state.alert_ids, ensure_ascii=False, separators=(",", ":")
        ),
        "feature": state.feature,
        "segment": state.segment,
        "opened_at": state.opened_at,
        "status": state.status,
        "event_at": state.event_at,
        "available_at": state.available_at,
    }


def compute_incidents(
    alerts: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    episode_id: str,
    decision_source: DecisionSource | None = None,
) -> IncidentResult:
    """Group P3.2 alerts into versioned incident rows for one episode.

    Alert and decision events are processed chronologically. Decisions take
    effect at ``decision_at``; for equal-time events, a decision for an existing
    incident is applied before alerts, and a decision for an incident first
    opened at that time is applied immediately after that incident is created.
    """
    episode_id = _validate_episode_id(episode_id)
    alert_rows = _validate_alerts(alerts, episode_id=episode_id)
    decisions = _load_decisions(decision_source, episode_id=episode_id)

    alerts_by_time: dict[datetime, list[_Alert]] = {}
    decisions_by_time: dict[datetime, list[_Decision]] = {}
    for alert in alert_rows:
        alerts_by_time.setdefault(alert.as_of, []).append(alert)
    for decision in decisions:
        decisions_by_time.setdefault(decision.decision_at, []).append(decision)
    event_times = sorted(set(alerts_by_time) | set(decisions_by_time))

    states: dict[str, _IncidentState] = {}
    snapshots: list[dict[str, Any]] = []
    pending_decisions: list[_Decision] = []

    def apply_decision(decision: _Decision, dirty: set[str]) -> None:
        incident_id = decision.row["incident_id"]
        state = states.get(incident_id)
        if state is None:
            pending_decisions.append(decision)
            return
        if decision.decision_at < state.opened_at_dt:
            raise MonitoringIncidentsError(
                f"decision {decision.row['decision_id']!r} predates incident {incident_id!r}"
            )
        if state.status in FINAL_ACTIONS:
            # A final decision is immutable; later source rows cannot reopen or replace it.
            return
        action = decision.row["action"]
        state.status = action
        state.event_at = decision.row["decision_at"]
        state.available_at = decision.row["decision_at"]
        if action in FINAL_ACTIONS:
            state.final_decision_at = decision.decision_at
            state.final_decision_id = decision.row["decision_id"]
        dirty.add(incident_id)

    def apply_pending_for_known_incidents(dirty: set[str]) -> None:
        if not pending_decisions:
            return
        remaining: list[_Decision] = []
        for decision in pending_decisions:
            if decision.row["incident_id"] in states:
                apply_decision(decision, dirty)
            else:
                remaining.append(decision)
        pending_decisions[:] = remaining

    def create_or_update_for_alert(alert: _Alert, dirty: set[str]) -> _IncidentState:
        row = alert.row
        scope = (row["feature"], row["segment"])
        active_candidates = [
            state
            for state in states.values()
            if (state.feature, state.segment) == scope
            and state.status in {"open", "recheck"}
            and state.latest_alert_at is not None
            and timedelta(0) <= alert.as_of - state.latest_alert_at <= _MERGE_WINDOW
        ]
        if active_candidates:
            state = max(
                active_candidates,
                key=lambda item: (item.latest_alert_at, item.opened_at_dt, item.incident_id),
            )
            state.alert_ids.append(row["alert_id"])
            state.latest_alert_at = alert.as_of
            state.event_at = row["event_at"]
            state.available_at = max(state.available_at, row["available_at"])
            dirty.add(state.incident_id)
            return state

        finalized_candidates = [
            state
            for state in states.values()
            if (state.feature, state.segment) == scope
            and state.status in FINAL_ACTIONS
            and state.latest_alert_at is not None
            and timedelta(0) <= alert.as_of - state.latest_alert_at <= _MERGE_WINDOW
        ]
        parent_incident_id: str | None = None
        if finalized_candidates:
            parent = max(
                finalized_candidates,
                key=lambda item: (
                    item.latest_alert_at,
                    item.final_decision_at or item.opened_at_dt,
                    item.incident_id,
                ),
            )
            parent_incident_id = parent.incident_id

        incident_id = _incident_id(
            episode_id=episode_id,
            first_alert=row,
            parent_incident_id=parent_incident_id,
        )
        if incident_id in states:
            raise MonitoringIncidentsError(
                f"deterministic incident ID collision for alert {row['alert_id']!r}"
            )
        state = _IncidentState(
            incident_id=incident_id,
            parent_incident_id=parent_incident_id,
            episode_id=episode_id,
            feature=row["feature"],
            segment=row["segment"],
            opened_at=row["as_of_time"],
            opened_at_dt=alert.as_of,
            alert_ids=[row["alert_id"]],
            latest_alert_at=alert.as_of,
            event_at=row["event_at"],
            available_at=row["available_at"],
        )
        states[incident_id] = state
        dirty.add(incident_id)
        return state

    for event_at in event_times:
        dirty: set[str] = set()
        pending_decisions.extend(decisions_by_time.get(event_at, ()))
        apply_pending_for_known_incidents(dirty)

        for alert in alerts_by_time.get(event_at, ()):
            create_or_update_for_alert(alert, dirty)
            # This also applies a decision at the opening timestamp once its
            # deterministic incident ID becomes available.
            apply_pending_for_known_incidents(dirty)

        for incident_id in sorted(dirty):
            snapshots.append(_snapshot(states[incident_id]))

    if pending_decisions:
        missing = sorted({item.row["incident_id"] for item in pending_decisions})
        raise MonitoringIncidentsError(
            "decision source returned incident IDs absent from this episode's alerts: "
            + ", ".join(missing)
        )

    snapshots.sort(
        key=lambda row: (
            row["event_at"],
            row["episode_id"],
            row["incident_id"],
        )
    )
    status_counts: dict[str, int] = {}
    for state in states.values():
        status_counts[state.status] = status_counts.get(state.status, 0) + 1
    report = {
        "episode_id": episode_id,
        "alert_count": len(alert_rows),
        "incident_count": len(states),
        "child_incident_count": sum(
            state.parent_incident_id is not None for state in states.values()
        ),
        "snapshot_count": len(snapshots),
        "latest_status_counts": dict(sorted(status_counts.items())),
    }
    return IncidentResult(incidents=tuple(snapshots), report=report)


def incidents_csv(rows: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> str:
    """Serialize versioned incidents using the documented output schema."""
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=INCIDENT_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        if set(row) != set(INCIDENT_COLUMNS):
            raise MonitoringIncidentsError("incident row does not match the output schema")
        writer.writerow(row)
    return stream.getvalue()


def write_incidents_csv(
    rows: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    destination: str | Path | TextIO,
) -> None:
    """Write incidents as CSV to a path or open text stream."""
    contents = incidents_csv(rows)
    if hasattr(destination, "write"):
        destination.write(contents)
        return
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8", newline="")
