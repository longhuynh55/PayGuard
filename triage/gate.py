"""Pure action gate for an operational incident snapshot."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import math
from typing import Any

from triage.decisions import CHECKLIST_RESULT_STATUSES, validate_decision_row


_INCIDENT_FIELDS = frozenset(
    {
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
    }
)
_INCIDENT_STATUSES = frozenset(
    {"open", "recheck", "close", "data_ticket", "fraud_ops", "human_review"}
)
_CHECKLIST_STATUSES = CHECKLIST_RESULT_STATUSES
_CHECKLIST_FIELDS = frozenset(
    {
        "status",
        "reason",
        "scope_match",
        "window_complete",
        "requires_human_review",
        "details",
        # This is intentionally discarded: evidence status belongs to code.
        "evidence_status",
    }
)
_BENIGN_CAUSES = frozenset({"seasonal"})
_BENIGN_RUNBOOKS = frozenset({"seasonal_calendar"})
_TIMESTAMP_FIELDS = ("opened_at", "event_at", "available_at")


class GateInputError(ValueError):
    """Raised when the caller supplies invalid policy or decision context."""


@dataclass(frozen=True)
class GateConfig:
    """Immutable policy settings consumed by the pure gate."""

    policy_version: str
    close_enabled: Mapping[str, bool]
    agent_close_disabled: bool = True
    tau_agent: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.policy_version, str) or not self.policy_version.strip():
            raise GateInputError("policy_version must be non-empty text")
        if not isinstance(self.close_enabled, Mapping):
            raise GateInputError("close_enabled must be a mapping by runbook")
        for runbook, enabled in self.close_enabled.items():
            if not isinstance(runbook, str) or not runbook.strip():
                raise GateInputError("close_enabled keys must be non-empty text")
            if not isinstance(enabled, bool):
                raise GateInputError("close_enabled values must be booleans")
        if not isinstance(self.agent_close_disabled, bool):
            raise GateInputError("agent_close_disabled must be a boolean")
        if self.tau_agent is not None:
            if (
                isinstance(self.tau_agent, bool)
                or not isinstance(self.tau_agent, (int, float))
                or not math.isfinite(float(self.tau_agent))
                or not any(
                    math.isclose(float(self.tau_agent), allowed, abs_tol=1e-12)
                    for allowed in (2 / 3, 1.0)
                )
            ):
                raise GateInputError("tau_agent must be NULL, 2/3, or 1")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> GateConfig:
        """Build settings from the plain mapping loaded from triage.yaml."""

        if not isinstance(value, Mapping):
            raise GateInputError("triage config must be a mapping")
        try:
            return cls(
                policy_version=value["policy_version"],
                close_enabled=value["close_enabled"],
                agent_close_disabled=value["agent_close_disabled"],
                tau_agent=value.get("tau_agent"),
            )
        except KeyError as exc:
            raise GateInputError(f"triage config is missing {exc.args[0]!r}") from exc


def _parse_timestamp(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a timezone-aware timestamp")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    if parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError(f"{field_name} must be UTC")
    return parsed.astimezone(UTC)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _nonempty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _check_snapshot(
    incident: Any, *, expected_episode_id: str, as_of: datetime
) -> tuple[bool, str | None, dict[str, Any]]:
    if not isinstance(incident, Mapping):
        return False, "Incident snapshot is not an object.", {}
    row = dict(incident)
    if set(row) != _INCIDENT_FIELDS:
        return False, "Incident snapshot does not match the incidents schema.", row

    for field_name in (
        "incident_id",
        "episode_id",
        "feature",
        "segment",
        "status",
        "alert_ids_json",
    ):
        if not _nonempty_text(row.get(field_name)):
            return False, f"Incident snapshot has an invalid {field_name}.", row
    if row["parent_incident_id"] is not None and not _nonempty_text(
        row["parent_incident_id"]
    ):
        return False, "Incident snapshot has an invalid parent_incident_id.", row
    if not isinstance(row["status"], str) or row["status"] not in _INCIDENT_STATUSES:
        return False, "Incident snapshot has an unsupported status.", row
    if row["episode_id"] == "base":
        return False, "Incident snapshot cannot belong to the base episode.", row
    if row["episode_id"] != expected_episode_id:
        return False, "Incident snapshot episode_id does not match the expected episode.", row

    try:
        alert_ids = json.loads(row["alert_ids_json"])
    except (TypeError, json.JSONDecodeError):
        return False, "Incident snapshot has invalid alert_ids_json.", row
    if not isinstance(alert_ids, list) or not alert_ids or any(
        not _nonempty_text(alert_id) for alert_id in alert_ids
    ):
        return False, "Incident snapshot alert_ids_json must be a non-empty string array.", row

    parsed_times: dict[str, datetime] = {}
    for field_name in _TIMESTAMP_FIELDS:
        try:
            parsed_times[field_name] = _parse_timestamp(row[field_name], field_name)
        except ValueError as exc:
            return False, str(exc), row
    if parsed_times["opened_at"] > parsed_times["event_at"]:
        return False, "Incident snapshot event_at predates opened_at.", row
    if parsed_times["event_at"] > parsed_times["available_at"]:
        return False, "Incident snapshot is available before its event_at.", row
    if any(value > as_of for value in parsed_times.values()):
        return False, "Incident snapshot contains an event after as_of_time.", row
    if parsed_times["available_at"] > as_of:
        return False, "Incident snapshot is not available at as_of_time.", row
    return True, None, row


def _normalize_checklists(value: Any) -> tuple[dict[str, dict[str, Any]], list[str]]:
    if value is None:
        return {}, []
    if not isinstance(value, Mapping):
        return {}, ["Checklist results must be an object."]

    normalized: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for name in sorted(value, key=lambda item: str(item)):
        result = value[name]
        if not _nonempty_text(name) or not isinstance(result, Mapping):
            errors.append("Checklist result has an invalid name or value.")
            continue
        status = result.get("status")
        if not isinstance(status, str) or status not in _CHECKLIST_STATUSES:
            errors.append(f"Checklist {name!r} has an invalid status.")
            continue

        item: dict[str, Any] = {"status": status}
        for field_name in ("scope_match", "window_complete", "requires_human_review"):
            if field_name in result:
                if not isinstance(result[field_name], bool):
                    errors.append(f"Checklist {name!r} has an invalid {field_name} flag.")
                    continue
                item[field_name] = result[field_name]
        if "reason" in result:
            if not isinstance(result["reason"], str):
                errors.append(f"Checklist {name!r} has an invalid reason.")
            elif result["reason"].strip():
                item["reason"] = result["reason"].strip()
        if "details" in result:
            try:
                # JSON round-trip rejects unserializable values and NaN/Infinity.
                item["details"] = json.loads(
                    json.dumps(result["details"], ensure_ascii=False, allow_nan=False)
                )
            except (TypeError, ValueError):
                errors.append(f"Checklist {name!r} has non-JSON details.")
        unknown = set(result) - _CHECKLIST_FIELDS
        if unknown:
            errors.append(f"Checklist {name!r} contains unsupported fields.")
        normalized[name] = item
    return normalized, errors


def _evidence_status(
    checklist_results: Mapping[str, Mapping[str, Any]],
    *,
    action: str,
    runbook: str | None,
    tool_error: bool,
    snapshot_valid: bool,
    checklist_errors: Sequence[str],
) -> str:
    statuses = [item["status"] for item in checklist_results.values()]
    if "conflicting" in statuses:
        return "conflicting"

    if action == "close":
        relevant_names = [runbook] if runbook else []
    elif action == "data_ticket":
        relevant_names = ["unit_or_null_change"]
    elif action == "fraud_ops":
        relevant_names = ["fraud_signal"]
    elif action == "recheck":
        relevant_names = [runbook] if runbook else list(checklist_results)
    else:
        relevant_names = list(checklist_results)

    if (
        not relevant_names
        or tool_error
        or not snapshot_valid
        or checklist_errors
    ):
        return "insufficient"

    for name in relevant_names:
        item = checklist_results.get(name)
        if item is None or item["status"] != "pass":
            return "insufficient"
        if item.get("scope_match") is False or item.get("window_complete") is False:
            return "insufficient"
        if action == "fraud_ops" and (
            item.get("scope_match") is not True
            or item.get("window_complete") is not True
        ):
            return "insufficient"

    if action in {"close", "data_ticket"} and runbook in {
        "seasonal_calendar",
        "unit_or_null_change",
    }:
        fraud_signal = checklist_results.get("fraud_signal")
        if (
            fraud_signal is not None
            and fraud_signal["status"] not in {"fail", "not_applicable"}
            and fraud_signal.get("scope_match") is not False
            and (
                fraud_signal["status"] != "pass"
                or fraud_signal.get("scope_match") is not True
                or fraud_signal.get("window_complete") is False
            )
        ):
            return "insufficient"
    return "sufficient"


def _mandatory_runbook_reasons(
    runbook: str | None,
    checklist_results: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    applicable_runbooks = {
        name
        for name in ("seasonal_calendar", "unit_or_null_change")
        if name in checklist_results
        and checklist_results[name]["status"] != "not_applicable"
    }
    if runbook in {"seasonal_calendar", "unit_or_null_change"}:
        applicable_runbooks.add(runbook)
    if not applicable_runbooks:
        return []
    fraud_result = checklist_results.get("fraud_signal")
    if (
        fraud_result is not None
        and fraud_result.get("status") == "pass"
        and fraud_result.get("scope_match") is True
    ):
        return [
            f"Runbook {name!r} requires human review for a fraud signal in the same segment."
            for name in sorted(applicable_runbooks)
        ]
    return []


def _as_reasons(value: Any) -> tuple[list[str], bool]:
    if value is None:
        return [], False
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return [], True
    reasons = [reason.strip() for reason in value if isinstance(reason, str) and reason.strip()]
    invalid = len(reasons) != len(value)
    return sorted(set(reasons)), invalid


def _usage_columns(value: Any) -> dict[str, int | float | None]:
    columns: dict[str, int | float | None] = {
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "cost_usd": None,
    }
    if value is None:
        return columns
    if not isinstance(value, Mapping):
        raise GateInputError("usage must be a mapping or NULL")
    unknown = set(value) - set(columns)
    if unknown:
        raise GateInputError(f"usage contains unsupported fields: {sorted(unknown)}")
    for field_name in ("input_tokens", "output_tokens", "total_tokens"):
        item = value.get(field_name)
        if item is not None and (
            isinstance(item, bool) or not isinstance(item, int) or item < 0
        ):
            raise GateInputError(f"{field_name} must be a non-negative integer or NULL")
        columns[field_name] = item
    cost = value.get("cost_usd")
    if cost is not None and (
        isinstance(cost, bool)
        or not isinstance(cost, (int, float))
        or not math.isfinite(float(cost))
        or cost < 0
    ):
        raise GateInputError("cost_usd must be a non-negative number or NULL")
    columns["cost_usd"] = cost
    return columns


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _fallback_incident_id(incident: Mapping[str, Any], *, episode_id: str, as_of: str) -> str:
    known = {
        field: incident.get(field)
        for field in sorted(_INCIDENT_FIELDS)
        if isinstance(incident.get(field), (str, type(None)))
    }
    canonical = _canonical_json(
        {"schema_version": 1, "episode_id": episode_id, "as_of_time": as_of, "snapshot": known}
    )
    return "incident-invalid-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def _result_reason(
    requested_action: str,
    *,
    proposal: Mapping[str, Any],
    explicit_recheck_reason: Any,
    checklist_results: Mapping[str, Mapping[str, Any]],
    runbook: str | None,
) -> str | None:
    candidates: list[Any] = [explicit_recheck_reason, proposal.get("recheck_reason")]
    if requested_action == "recheck":
        candidates.append(proposal.get("reason"))
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    relevant = checklist_results.get(runbook) if runbook else None
    pending_reasons = []
    if relevant is not None and (
        relevant["status"] == "pending" or relevant.get("window_complete") is False
    ) and isinstance(relevant.get("reason"), str):
        pending_reasons.append(relevant["reason"])
    if pending_reasons:
        return "; ".join(sorted(set(pending_reasons)))
    return None


def evaluate_gate(
    incident: Mapping[str, Any],
    proposal: Mapping[str, Any],
    *,
    source: str,
    expected_episode_id: str,
    as_of_time: str,
    config: GateConfig | Mapping[str, Any],
    mandatory_human_review: bool = False,
    mandatory_review_reasons: Sequence[str] = (),
    tool_error: bool = False,
    tool_error_reason: str | None = None,
    recheck_reason: str | None = None,
    close_score: float | None = None,
    usage: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return one deterministic decision row without reading or mutating state.

    ``evidence_status`` is derived only from normalized checklist/tool results and
    snapshot validity. Any same-named field in the proposal or checklist payload
    is discarded. ``as_of_time`` and ``expected_episode_id`` are explicit because
    an incidents row has no separate as-of column.
    """

    if not isinstance(source, str) or source not in {"rule", "agent"}:
        raise GateInputError("source must be rule or agent")
    if not isinstance(expected_episode_id, str) or not expected_episode_id.strip() or expected_episode_id == "base":
        raise GateInputError("expected_episode_id must identify a non-base episode")
    try:
        as_of = _parse_timestamp(as_of_time, "as_of_time")
    except ValueError as exc:
        raise GateInputError(str(exc)) from exc
    normalized_as_of = _format_timestamp(as_of)
    policy = config if isinstance(config, GateConfig) else GateConfig.from_mapping(config)
    usage_fields = _usage_columns(usage)

    if not isinstance(proposal, Mapping):
        proposal = {}
        proposal_errors = ["Proposal is not an object."]
    else:
        proposal_errors = []

    snapshot_valid, snapshot_error, snapshot = _check_snapshot(
        incident, expected_episode_id=expected_episode_id, as_of=as_of
    )
    normalized_checklists, checklist_errors = _normalize_checklists(
        proposal.get("checklist_results")
    )
    checklist_tool_error = any(
        item["status"] == "error" for item in normalized_checklists.values()
    )

    if not isinstance(tool_error, bool):
        tool_error = True
        proposal_errors.append("Tool error flag is invalid.")
    tool_failure = tool_error or checklist_tool_error
    tool_error_reasons: list[str] = []
    if checklist_tool_error:
        tool_error_reasons.append("A checklist tool reported an error.")
    if tool_error:
        tool_error_reasons.append(
            tool_error_reason.strip()
            if isinstance(tool_error_reason, str) and tool_error_reason.strip()
            else "A tool reported an error."
        )

    explicit_review_reasons, bad_review_reasons = _as_reasons(mandatory_review_reasons)
    mandatory_review = mandatory_human_review is True or bool(explicit_review_reasons)
    if not isinstance(mandatory_human_review, bool):
        mandatory_review = True
        proposal_errors.append("Mandatory human review flag is invalid.")
    if bad_review_reasons:
        mandatory_review = True
        explicit_review_reasons.append("Mandatory review reasons are malformed.")
    for name, result in normalized_checklists.items():
        if result.get("requires_human_review") is True:
            mandatory_review = True
            explicit_review_reasons.append(
                result.get("reason") or f"Checklist {name!r} requires human review."
            )

    cause = proposal.get("cause")
    if not _nonempty_text(cause):
        cause = "other"
        proposal_errors.append("Proposal has no valid cause.")
    runbook_value = proposal.get("runbook")
    if runbook_value is None or _nonempty_text(runbook_value):
        runbook = runbook_value
    else:
        runbook = None
        proposal_errors.append("Proposal has an invalid runbook.")

    requested_action = proposal.get("action")
    if not isinstance(requested_action, str) or requested_action not in {
        "close",
        "recheck",
        "data_ticket",
        "fraud_ops",
        "human_review",
    }:
        requested_action = "human_review"
        proposal_errors.append("Proposal has an unsupported action.")

    evidence_status = _evidence_status(
        normalized_checklists,
        action=requested_action,
        runbook=runbook,
        tool_error=tool_failure,
        snapshot_valid=snapshot_valid,
        checklist_errors=checklist_errors,
    )
    if evidence_status == "conflicting":
        explicit_review_reasons.append("Checklist evidence is conflicting.")
        mandatory_review = True
    runbook_review_reasons = _mandatory_runbook_reasons(
        runbook, normalized_checklists
    )
    if runbook_review_reasons:
        mandatory_review = True
        explicit_review_reasons.extend(runbook_review_reasons)

    traces_value = proposal.get("tool_trace_ids", [])
    if not isinstance(traces_value, (list, tuple)) or any(
        not _nonempty_text(trace_id) for trace_id in traces_value
    ):
        tool_trace_ids: list[str] = []
        proposal_errors.append("Proposal has invalid tool_trace_ids.")
    else:
        tool_trace_ids = list(traces_value)

    if checklist_errors:
        proposal_errors.extend(checklist_errors)
    if proposal_errors:
        mandatory_review = True

    score: float | None = None
    invalid_score = False
    if source == "agent" and close_score is not None:
        if (
            isinstance(close_score, bool)
            or not isinstance(close_score, (int, float))
            or not math.isfinite(float(close_score))
            or not 0 <= float(close_score) <= 1
        ):
            invalid_score = True
        else:
            score = float(close_score)

    reasons: list[str] = []
    if not snapshot_valid and snapshot_error:
        reasons.append(snapshot_error)
    reasons.extend(tool_error_reasons)
    reasons.extend(explicit_review_reasons)
    reasons.extend(proposal_errors)

    decision_action = requested_action
    recheck_why = _result_reason(
        requested_action,
        proposal=proposal,
        explicit_recheck_reason=recheck_reason,
        checklist_results=normalized_checklists,
        runbook=runbook,
    )

    if not snapshot_valid or tool_failure or proposal_errors or mandatory_review:
        decision_action = "human_review"
    elif requested_action == "human_review":
        decision_action = "human_review"
        reasons.append(
            proposal.get("reason", "Proposal requests human review.")
            if isinstance(proposal.get("reason"), str) and proposal.get("reason", "").strip()
            else "Proposal requests human review."
        )
    elif requested_action == "recheck":
        if recheck_why:
            decision_action = "recheck"
            reasons.append(recheck_why)
        else:
            decision_action = "human_review"
            reasons.append("Recheck requires a recorded reason for waiting on evidence.")
    elif requested_action in {"data_ticket", "fraud_ops"}:
        if requested_action == "data_ticket":
            unit_result = normalized_checklists.get("unit_or_null_change", {})
            fraud_result = normalized_checklists.get("fraud_signal")
            fraud_conflict = bool(
                fraud_result
                and fraud_result["status"] in {"pass", "pending"}
                and fraud_result.get("scope_match") is not False
            )
            if (
                runbook != "unit_or_null_change"
                or unit_result.get("status") != "pass"
                or unit_result.get("scope_match") is False
                or unit_result.get("window_complete") is False
            ):
                decision_action = "human_review"
                reasons.append(
                    "data_ticket requires a passing unit_or_null_change checklist."
                )
            elif fraud_conflict:
                decision_action = "human_review"
                reasons.append("A fraud signal overlaps or may overlap this incident scope.")
            else:
                decision_action = "data_ticket"
                reasons.append("The unit_or_null_change routing checklist passed.")
        else:
            fraud_result = normalized_checklists.get("fraud_signal", {})
            if (
                fraud_result.get("status") == "pass"
                and fraud_result.get("scope_match") is True
                and fraud_result.get("window_complete") is True
            ):
                decision_action = "fraud_ops"
                reasons.append(
                    "A structured fraud signal passed for this scope and observation window."
                )
            else:
                decision_action = "human_review"
                reasons.append(
                    "fraud_ops requires an in-scope structured signal and a complete window."
                )
    elif requested_action == "close":
        runbook_check = normalized_checklists.get(runbook, {}) if runbook else {}
        if runbook not in _BENIGN_RUNBOOKS or cause not in _BENIGN_CAUSES:
            decision_action = "human_review"
            reasons.append(
                "Only seasonal cause with the seasonal_calendar runbook may enter close."
            )
        elif runbook_check.get("scope_match") is False:
            decision_action = "human_review"
            reasons.append("The benign checklist does not match the incident scope.")
        elif runbook_check.get("status") != "pass" or evidence_status != "sufficient":
            if recheck_why:
                decision_action = "recheck"
                reasons.append(recheck_why)
            else:
                decision_action = "human_review"
                reasons.append("The benign checklist did not pass with sufficient evidence.")
        elif source == "rule":
            if policy.close_enabled.get(runbook, False):
                decision_action = "close"
                reasons.append(f"The benign checklist passed and {runbook!r} may close.")
            elif recheck_why:
                decision_action = "recheck"
                reasons.append(recheck_why)
            else:
                decision_action = "human_review"
                reasons.append(f"Runbook {runbook!r} is not enabled to close.")
        elif policy.agent_close_disabled or policy.tau_agent is None:
            if recheck_why:
                decision_action = "recheck"
                reasons.append(recheck_why)
            else:
                decision_action = "human_review"
                reasons.append("Agent closing is disabled or has no configured tau_agent.")
        elif invalid_score or score is None or score < float(policy.tau_agent):
            if recheck_why:
                decision_action = "recheck"
                reasons.append(recheck_why)
            else:
                decision_action = "human_review"
                reasons.append("Agent close_score does not meet tau_agent.")
        else:
            decision_action = "close"
            reasons.append("The benign checklist passed and agent close_score met tau_agent.")

    if not reasons:
        proposal_reason = proposal.get("reason")
        if isinstance(proposal_reason, str) and proposal_reason.strip():
            reasons.append(proposal_reason.strip())
        else:
            reasons.append(f"Decision followed the {requested_action} action gate.")
    reason = "; ".join(sorted(set(reasons)))

    observed_episode_id = snapshot.get("episode_id")
    episode_id = (
        observed_episode_id
        if _nonempty_text(observed_episode_id) and observed_episode_id != "base"
        else expected_episode_id
    )
    incident_id = snapshot.get("incident_id")
    if not _nonempty_text(incident_id):
        incident_id = _fallback_incident_id(
            snapshot, episode_id=episode_id, as_of=normalized_as_of
        )
    if snapshot_valid:
        feature = snapshot["feature"]
        segment = snapshot["segment"]
        opened_at = snapshot["opened_at"]
    else:
        feature = None
        segment = None
        opened_at = None
    scope_json = _canonical_json(
        {
            "feature": feature,
            "segment": segment,
            "start_at": opened_at,
            "end_at": normalized_as_of,
        }
    )
    checklist_json = _canonical_json(normalized_checklists)
    traces_json = _canonical_json(tool_trace_ids)
    conclusion = (
        f"cause={cause}; proposed_action={requested_action}; "
        f"action={decision_action}; evidence_status={evidence_status}"
    )
    decision_payload = {
        "incident_id": incident_id,
        "episode_id": episode_id,
        "decision_at": normalized_as_of,
        "as_of_time": normalized_as_of,
        "source": source,
        "runbook": runbook,
        "cause": cause,
        "evidence_status": evidence_status,
        "action": decision_action,
        "scope_json": scope_json,
        "checklist_results_json": checklist_json,
        "tool_trace_ids_json": traces_json,
        "conclusion": conclusion,
        "reason": reason,
        "close_score": score,
        "policy_version": policy.policy_version,
        **usage_fields,
    }
    identity = {"schema_version": 1, "decision": decision_payload}
    decision_id = "decision-" + hashlib.sha256(
        _canonical_json(identity).encode("utf-8")
    ).hexdigest()[:24]
    row = {"decision_id": decision_id, **decision_payload}
    return validate_decision_row(row)
