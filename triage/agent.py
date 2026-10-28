"""One-step investigation agent and its offline provider choices."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from typing import Any, Protocol

from triage.llm import (
    DEFAULT_CONFIG_PATH,
    FakeLLMClient,
    LLMCallRecord,
    LLMClient,
    LLMResult,
    load_llm_config,
)
from triage.tools import run_sql


CAUSES = frozenset(
    {
        "seasonal",
        "legit_business_change",
        "no_material_change",
        "data_pipeline",
        "fraud_surge",
        "new_fraud",
        "other",
    }
)
RUNBOOKS = frozenset(
    {"seasonal_calendar", "unit_or_null_change", "small_sample", "segment_concentration"}
)
ACTIONS = frozenset({"close", "recheck", "data_ticket", "fraud_ops", "human_review"})
_INCIDENT_CONTEXT_FIELDS = (
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
class AgentProposalProvider(Protocol):
    """Provide a cause/action proposal for an incident not handled by a rule."""

    @property
    def model_slug(self) -> str | None:
        """Return the canonical model slug, or NULL when no LLM is configured."""

    def propose(
        self,
        incident: Mapping[str, Any],
        *,
        as_of_time: str,
        checklist_results: Mapping[str, Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        """Return proposal fields consumed by the code-owned gate."""


class UnconfiguredAgent:
    """Safe round-one fallback until an LLM provider is selected explicitly."""

    model_slug = None

    def propose(
        self,
        incident: Mapping[str, Any],
        *,
        as_of_time: str,
        checklist_results: Mapping[str, Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        del incident, as_of_time
        return {
            "cause": "other",
            "runbook": None,
            "action": "human_review",
            "checklist_results": checklist_results,
            "reason": "agent_not_configured",
        }


class OneStepAgent:
    """Run one as-of SQL tool and one JSON proposal call for an incident."""

    def __init__(self, operational_db: str | Path, client: LLMClient) -> None:
        self.operational_db = Path(operational_db).expanduser().resolve()
        self.client = client
        self.last_call_reports: list[dict[str, Any]] = []

    @property
    def model_slug(self) -> str:
        return self.client.config.canonical_slug

    def propose(
        self,
        incident: Mapping[str, Any],
        *,
        as_of_time: str,
        checklist_results: Mapping[str, Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        self.last_call_reports = []
        episode_id = incident.get("episode_id")
        safe_incident = {field: incident.get(field) for field in _INCIDENT_CONTEXT_FIELDS}
        safe_checklists = _json_safe(checklist_results)
        question = (
            "Investigate the incident using one read-only operational query. "
            "Use this incident snapshot and the code-computed rule checklist as context; "
            "only query facts visible in the supplied as-of snapshot. Return a compact "
            "result that may help classify the cause. Context: "
            + _canonical_json(
                {"incident": safe_incident, "rule_checklist_results": safe_checklists}
            )
        )
        sql_result = run_sql(
            question,
            str(episode_id),
            as_of_time,
            operational_db=self.operational_db,
            client=self.client,
        )

        proposal_context = {
            "incident": safe_incident,
            "as_of_time": as_of_time,
            "rule_checklist_results": safe_checklists,
            "run_sql": {
                "ok": sql_result.ok,
                "rows": sql_result.rows,
                "trace": sql_result.trace.as_dict(),
            },
        }
        prompt = (
            "You are a fraud monitoring incident investigator. Use only the operational "
            "incident, code-computed rule checklist, and single run_sql result below. "
            "Do not invent facts. Return exactly one JSON object with exactly these keys: "
            '"cause" (one of seasonal, legit_business_change, no_material_change, '
            'data_pipeline, fraud_surge, new_fraud, other), "action" (one of close, recheck, '
            'data_ticket, fraud_ops, human_review), and "reason" (a non-empty string). '
            "You may include a runbook key with one of seasonal_calendar, "
            "unit_or_null_change, small_sample, segment_concentration, or null. "
            "Do not include evidence_status, close_score, "
            "scope, checklist results, or tool trace IDs. A failed SQL result or missing "
            "evidence should lead to human_review.\n\nContext:\n"
            + _canonical_json(proposal_context)
        )
        snapshot_material = _canonical_json(
            {
                "tool_snapshot": sql_result.trace.data_snapshot,
                "incident": safe_incident,
                "rule_checklist_results": safe_checklists,
                "sql_trace": sql_result.trace.as_dict(),
                "sql_rows": sql_result.rows,
            }
        )
        sql_llm_error = None
        sql_tool_status = None
        if sql_result.trace.error is not None:
            sql_tool_status = sql_result.trace.error.code
            if sql_result.trace.error.code in _LLM_ERROR_CODES:
                sql_llm_error = sql_result.trace.error.code
        proposal_result: LLMResult | None = None
        if sql_llm_error is None:
            proposal_result = self.client.complete_json(
                prompt,
                episode_id=str(episode_id),
                as_of_time=as_of_time,
                data_snapshot=hashlib.sha256(snapshot_material.encode("utf-8")).hexdigest(),
            )
            proposal, proposal_error = _normalize_proposal(proposal_result)
        else:
            # An API-level failure is terminal for this measurement. Do not
            # issue the second request after authentication/provider failures.
            proposal = _fallback_proposal(sql_llm_error)
            proposal_error = sql_llm_error

        self.last_call_reports = [
            _call_report(
                "run_sql_generation",
                sql_result.trace.llm,
                error_status=sql_llm_error,
                tool_status=sql_tool_status,
                canonical_slug=self.model_slug,
            )
        ]
        if proposal_result is not None:
            self.last_call_reports.append(
                _call_report(
                    "proposal",
                    proposal_result.record,
                    error_status=(
                        proposal_result.error.code if proposal_result.error else None
                    ),
                    tool_status=proposal_error,
                    canonical_slug=self.model_slug,
                )
            )
        records = [
            record
            for record in (
                sql_result.trace.llm,
                proposal_result.record if proposal_result is not None else None,
            )
            if record is not None
        ]
        usage = _aggregate_usage(records)
        trace_details = {
            "tool_trace_id": sql_result.trace.tool_trace_id,
            "sql": sql_result.trace.sql,
            "row_count": sql_result.trace.row_count,
            "truncated": sql_result.trace.truncated,
            "data_snapshot": sql_result.trace.data_snapshot,
            "error": sql_result.trace.error.as_dict() if sql_result.trace.error else None,
        }
        code_checklists: dict[str, dict[str, Any]] = {
            "agent_run_sql": {
                "status": "pass" if sql_result.ok else "error",
                "reason": (
                    "One operational as-of SQL query completed."
                    if sql_result.ok
                    else sql_result.error.message if sql_result.error else "SQL tool failed."
                ),
                "scope_match": True,
                "window_complete": True,
                "details": trace_details,
            }
        }
        if proposal_error is not None:
            code_checklists["agent_proposal"] = {
                "status": "error",
                "reason": proposal_error,
                "scope_match": True,
                "window_complete": True,
                "details": (
                    {"cache_key": proposal_result.record.cache_key}
                    if proposal_result is not None
                    else {"error_status": proposal_error}
                ),
            }

        tool_error = not sql_result.ok or proposal_error is not None
        tool_error_reason = None
        if not sql_result.ok:
            tool_error_reason = (
                f"run_sql failed: {sql_result.error.code}"
                if sql_result.error
                else "run_sql failed."
            )
        elif proposal_error is not None:
            tool_error_reason = f"Agent proposal failed: {proposal_error}"

        proposal.update(
            {
                "tool_trace_ids": [sql_result.trace.tool_trace_id],
                "_agent_checklist_results": code_checklists,
                "_model_slug": self.model_slug,
                "_tool_error": tool_error,
                "_tool_error_reason": tool_error_reason,
                "_usage": usage,
            }
        )
        return proposal


_LLM_ERROR_CODES = frozenset(
    {
        "cost_limit_exceeded",
        "empty_content",
        "http_error",
        "invalid_json",
        "invalid_response",
        "missing_api_key",
        "network_error",
        "provider_error",
        "provider_unavailable",
        "timeout",
        "token_limit",
        "transport_error",
    }
)


def _call_report(
    request_type: str,
    record: LLMCallRecord | None,
    *,
    error_status: str | None,
    tool_status: str | None,
    canonical_slug: str,
) -> dict[str, Any]:
    """Return usage-only metadata safe for the public P4.6 measurement report."""
    return {
        "request_type": request_type,
        "input_tokens": record.prompt_tokens if record else 0,
        "output_tokens": record.completion_tokens if record else 0,
        "total_tokens": (
            record.prompt_tokens + record.completion_tokens if record else 0
        ),
        "cost_usd": record.cost_usd if record else 0.0,
        "latency_ms": record.latency_ms if record else 0.0,
        "model_canonical_slug": record.canonical_slug if record else canonical_slug,
        "error_status": error_status or ("not_called" if record is None else None),
        "tool_status": tool_status,
    }


def build_agent_provider(
    provider_name: str,
    operational_db: str | Path,
    *,
    llm_config_path: str | Path = DEFAULT_CONFIG_PATH,
) -> AgentProposalProvider:
    """Construct the explicitly selected CLI provider; network access is opt-in."""
    if provider_name == "unconfigured":
        return UnconfiguredAgent()
    if provider_name == "fake":
        base_config = load_llm_config(llm_config_path)
        fake_config = replace(
            base_config,
            model_id="fake/payguard-p4-round1",
            canonical_slug="fake/payguard-p4-round1",
            retry_count=0,
        )
        fake_client = FakeLLMClient(
            config=fake_config,
            response_factory=_fake_agent_response,
        )
        return OneStepAgent(operational_db, fake_client)
    if provider_name == "openrouter":
        return OneStepAgent(
            operational_db,
            LLMClient(config_path=llm_config_path),
        )
    raise ValueError(f"unsupported agent provider: {provider_name!r}")


def _fake_agent_response(payload: dict[str, Any]) -> Any:
    messages = payload.get("messages")
    prompt = messages[0].get("content", "") if isinstance(messages, list) and messages else ""
    if "field named sql" in prompt:
        return {"sql": "SELECT COUNT(*) AS transaction_count FROM asof.transactions"}
    return {
        "cause": "other",
        "runbook": None,
        "action": "human_review",
        "reason": "Deterministic fake agent requests human investigation.",
    }


def _normalize_proposal(result: LLMResult) -> tuple[dict[str, Any], str | None]:
    if not result.ok:
        assert result.error is not None
        return _fallback_proposal(result.error.code), result.error.code
    value = result.value
    if not isinstance(value, Mapping) or set(value) not in (
        {"cause", "action", "reason"},
        {"cause", "runbook", "action", "reason"},
    ):
        return _fallback_proposal("invalid_proposal_format"), "invalid_proposal_format"
    cause = value.get("cause")
    runbook = value.get("runbook")
    action = value.get("action")
    reason = value.get("reason")
    if (
        not isinstance(cause, str)
        or cause not in CAUSES
        or (runbook is not None and (not isinstance(runbook, str) or runbook not in RUNBOOKS))
    ):
        return _fallback_proposal("invalid_proposal_taxonomy"), "invalid_proposal_taxonomy"
    if (
        not isinstance(action, str)
        or action not in ACTIONS
        or not isinstance(reason, str)
        or not reason.strip()
    ):
        return _fallback_proposal("invalid_proposal_fields"), "invalid_proposal_fields"
    return {
        "cause": cause,
        "runbook": runbook,
        "action": action,
        "reason": reason.strip(),
    }, None


def _fallback_proposal(code: str) -> dict[str, Any]:
    return {
        "cause": "other",
        "runbook": None,
        "action": "human_review",
        "reason": f"Agent proposal unavailable ({code}); human review required.",
    }


def _aggregate_usage(records: list[LLMCallRecord]) -> dict[str, int | float] | None:
    if not records:
        return None
    input_tokens = sum(record.prompt_tokens for record in records)
    output_tokens = sum(record.completion_tokens for record in records)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "cost_usd": float(sum(record.cost_usd for record in records)),
    }


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
