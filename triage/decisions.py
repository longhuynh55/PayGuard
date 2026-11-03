"""Operational decision row contract for P4 triage."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import json
import math
import sqlite3
from typing import Any

from simulator.common import TimestampParseError, parse_timestamp
from simulator.storage import StorageError, insert_operational_record


DECISION_COLUMNS = (
    "decision_id",
    "incident_id",
    "episode_id",
    "decision_at",
    "as_of_time",
    "event_at",
    "available_at",
    "source",
    "runbook",
    "cause",
    "evidence_status",
    "action",
    "scope_json",
    "checklist_results_json",
    "tool_trace_ids_json",
    "conclusion",
    "reason",
    "close_score",
    "policy_version",
    "model_slug",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cost_usd",
)

DECISION_ACTIONS = frozenset(
    {"close", "recheck", "data_ticket", "fraud_ops", "human_review"}
)
EVIDENCE_STATUSES = frozenset({"sufficient", "insufficient", "conflicting"})
DECISION_SOURCES = frozenset({"rule", "agent"})
CHECKLIST_RESULT_STATUSES = frozenset(
    {"pass", "fail", "pending", "conflicting", "error", "not_applicable"}
)
CHECKLIST_RESULT_FIELDS = frozenset(
    {"status", "reason", "scope_match", "window_complete", "requires_human_review", "details"}
)


class DecisionSchemaError(ValueError):
    """Raised when a decision row does not match the operational contract."""


def normalize_decision_real_columns(row: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize SQLite REAL decision values before hashing or persistence."""

    normalized = dict(row)
    for field_name in ("close_score", "cost_usd"):
        value = normalized.get(field_name)
        if value is not None and not isinstance(value, bool) and isinstance(
            value, (int, float)
        ):
            normalized[field_name] = float(value)
    return normalized


def _utc_timestamp(value: Any, field_name: str) -> datetime:
    try:
        return parse_timestamp(value, utc=True)
    except TimestampParseError as exc:
        messages = {
            "not_text": "must be a UTC timestamp",
            "invalid": "must be a valid timestamp",
            "naive": "must be timezone-aware UTC",
            "not_utc": "must be timezone-aware UTC",
        }
        raise DecisionSchemaError(f"{field_name} {messages[exc.reason]}") from exc


def _nonempty_text(value: Any, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise DecisionSchemaError(f"{field_name} must be non-empty text")


def _json_value(value: Any, field_name: str, expected_type: type) -> Any:
    if not isinstance(value, str):
        raise DecisionSchemaError(f"{field_name} must be JSON text")
    try:
        decoded = json.loads(
            value,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON constant: {token}")
            ),
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DecisionSchemaError(f"{field_name} must contain valid JSON") from exc
    if not isinstance(decoded, expected_type):
        raise DecisionSchemaError(
            f"{field_name} must encode a {expected_type.__name__}"
        )
    return decoded


def validate_decision_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and copy a mapping against the P4 operational row schema."""

    if not isinstance(row, Mapping):
        raise DecisionSchemaError("decision row must be a mapping")
    if set(row) != set(DECISION_COLUMNS):
        missing = sorted(set(DECISION_COLUMNS) - set(row))
        extra = sorted(set(row) - set(DECISION_COLUMNS))
        raise DecisionSchemaError(
            f"decision row columns do not match schema (missing={missing}, extra={extra})"
        )

    copied = normalize_decision_real_columns(
        {column: row[column] for column in DECISION_COLUMNS}
    )
    for field_name in (
        "decision_id",
        "incident_id",
        "episode_id",
        "cause",
        "conclusion",
        "reason",
        "policy_version",
    ):
        _nonempty_text(copied[field_name], field_name)
    if copied["episode_id"] == "base":
        raise DecisionSchemaError("episode_id must not be 'base'")
    if not isinstance(copied["source"], str) or copied["source"] not in DECISION_SOURCES:
        raise DecisionSchemaError("source must be rule or agent")
    if not isinstance(copied["action"], str) or copied["action"] not in DECISION_ACTIONS:
        raise DecisionSchemaError("action is not a supported decision action")
    if (
        not isinstance(copied["evidence_status"], str)
        or copied["evidence_status"] not in EVIDENCE_STATUSES
    ):
        raise DecisionSchemaError("evidence_status is not supported")
    if copied["runbook"] is not None:
        _nonempty_text(copied["runbook"], "runbook")
    if copied["model_slug"] is not None:
        _nonempty_text(copied["model_slug"], "model_slug")
    if copied["source"] == "rule" and copied["model_slug"] is not None:
        raise DecisionSchemaError("model_slug must be NULL for rule decisions")

    decision_at = _utc_timestamp(copied["decision_at"], "decision_at")
    as_of_time = _utc_timestamp(copied["as_of_time"], "as_of_time")
    if decision_at != as_of_time:
        raise DecisionSchemaError("decision_at must equal as_of_time")
    event_at = _utc_timestamp(copied["event_at"], "event_at")
    available_at = _utc_timestamp(copied["available_at"], "available_at")
    if event_at != decision_at or available_at != decision_at:
        raise DecisionSchemaError(
            "event_at and available_at must equal decision_at for operational as-of reads"
        )

    scope = _json_value(copied["scope_json"], "scope_json", dict)
    for key in ("feature", "segment", "start_at", "end_at"):
        if key not in scope:
            raise DecisionSchemaError(f"scope_json is missing {key!r}")
        if scope[key] is not None and not isinstance(scope[key], str):
            raise DecisionSchemaError(f"scope_json {key!r} must be text or NULL")
    for key in ("start_at", "end_at"):
        if scope[key] is not None:
            _utc_timestamp(scope[key], f"scope_json.{key}")

    checklist_results = _json_value(
        copied["checklist_results_json"], "checklist_results_json", dict
    )
    for name, result in checklist_results.items():
        _nonempty_text(name, "checklist result name")
        if not isinstance(result, dict):
            raise DecisionSchemaError(f"checklist result {name!r} must be an object")
        if set(result) - CHECKLIST_RESULT_FIELDS:
            raise DecisionSchemaError(f"checklist result {name!r} has unsupported fields")
        status = result.get("status")
        if not isinstance(status, str) or status not in CHECKLIST_RESULT_STATUSES:
            raise DecisionSchemaError(f"checklist result {name!r} has an invalid status")
        if "reason" in result and not isinstance(result["reason"], str):
            raise DecisionSchemaError(f"checklist result {name!r} has an invalid reason")
        for key in ("scope_match", "window_complete", "requires_human_review"):
            if key in result and not isinstance(result[key], bool):
                raise DecisionSchemaError(f"checklist result {name!r} has an invalid {key}")
    trace_ids = _json_value(copied["tool_trace_ids_json"], "tool_trace_ids_json", list)
    if any(not isinstance(value, str) or not value.strip() for value in trace_ids):
        raise DecisionSchemaError("tool_trace_ids_json must contain non-empty strings")

    score = copied["close_score"]
    if score is not None:
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise DecisionSchemaError("close_score must be a number or NULL")
        if not math.isfinite(float(score)) or not 0 <= float(score) <= 1:
            raise DecisionSchemaError("close_score must be finite and in [0, 1]")

    for field_name in ("input_tokens", "output_tokens", "total_tokens"):
        value = copied[field_name]
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise DecisionSchemaError(f"{field_name} must be a non-negative integer or NULL")
    cost = copied["cost_usd"]
    if cost is not None and (
        isinstance(cost, bool)
        or not isinstance(cost, (int, float))
        or not math.isfinite(float(cost))
        or cost < 0
    ):
        raise DecisionSchemaError("cost_usd must be a non-negative number or NULL")

    return copied


def persist_decisions(
    connection: sqlite3.Connection, rows: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...]
) -> int:
    """Append decisions idempotently; changed content under an existing ID is rejected."""

    if not isinstance(rows, (list, tuple)):
        raise DecisionSchemaError("decision rows must be a list or tuple")
    inserted = 0
    connection.execute("SAVEPOINT persist_decisions")
    try:
        for index, source in enumerate(rows):
            try:
                row = validate_decision_row(source)
            except DecisionSchemaError as exc:
                raise DecisionSchemaError(f"decision row {index}: {exc}") from exc
            existing = connection.execute(
                "SELECT * FROM decisions WHERE decision_id = ?",
                (row["decision_id"],),
            ).fetchone()
            if existing is not None:
                if any(existing[column] != row[column] for column in DECISION_COLUMNS):
                    raise StorageError(
                        "decision_id already exists with different operational content"
                    )
                continue
            insert_operational_record(connection, "decisions", row)
            inserted += 1
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT persist_decisions")
        connection.execute("RELEASE SAVEPOINT persist_decisions")
        raise
    connection.execute("RELEASE SAVEPOINT persist_decisions")
    return inserted
