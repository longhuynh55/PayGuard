from __future__ import annotations

from dataclasses import replace
import json

import pytest

from triage.agent import OneStepAgent
from triage.gate import GateConfig, evaluate_gate
from triage.llm import FakeLLMClient, load_llm_config


AS_OF = "2026-02-02T00:00:00.000000Z"
INCIDENT = {
    "incident_id": "incident-a",
    "parent_incident_id": None,
    "episode_id": "ep-a",
    "alert_ids_json": '["alert-a"]',
    "feature": "amount_avg_7d",
    "segment": "retail",
    "opened_at": "2026-02-01T00:00:00.000000Z",
    "status": "open",
    "event_at": AS_OF,
    "available_at": AS_OF,
}
PASSING_SEASONAL = {
    "seasonal_calendar": {
        "status": "pass",
        "scope_match": True,
        "window_complete": True,
    }
}


def _gate(agent_result, *, checklist_results=None):
    result = dict(agent_result)
    tool_error = result.pop("_tool_error")
    tool_error_reason = result.pop("_tool_error_reason")
    usage = result.pop("_usage")
    model_slug = result.pop("_model_slug")
    code_checklists = result.pop("_agent_checklist_results")
    result["checklist_results"] = {
        **(checklist_results or {}),
        **code_checklists,
    }
    return evaluate_gate(
        INCIDENT,
        result,
        source="agent",
        expected_episode_id="ep-a",
        as_of_time=AS_OF,
        config=GateConfig(
            policy_version="test-agent-v1",
            close_enabled={},
            agent_close_disabled=True,
        ),
        tool_error=tool_error,
        tool_error_reason=tool_error_reason,
        usage=usage,
        model_slug=model_slug,
    )


def _agent(store, client):
    return OneStepAgent(store.operational_path, client)


def test_one_step_calls_one_sql_and_one_proposal_and_records_decision_usage(p1_2_store) -> None:
    client = FakeLLMClient(
        responses=[
            {
                "sql": (
                    "SELECT txn_id, episode_id FROM asof.transactions "
                    "ORDER BY txn_id"
                )
            },
            {
                "cause": "seasonal",
                "runbook": "seasonal_calendar",
                "action": "close",
                "reason": "The operational snapshot appears seasonal.",
            },
        ],
    )
    result = _agent(p1_2_store, client).propose(
        INCIDENT,
        as_of_time=AS_OF,
        checklist_results=PASSING_SEASONAL,
    )

    assert len(client.requests) == 2
    assert "field named sql" in client.requests[0]["messages"][0]["content"]
    proposal_prompt = client.requests[1]["messages"][0]["content"]
    assert "single run_sql result" in proposal_prompt
    assert "txn-base-before-t0" in proposal_prompt
    assert "txn-base-at-boundary" in proposal_prompt
    assert "txn-a-at-availability-cutoff" in proposal_prompt
    assert "txn-a-not-yet-available" not in proposal_prompt
    assert "txn-b-other-episode" not in proposal_prompt

    decision = _gate(result, checklist_results=PASSING_SEASONAL)
    assert decision["action"] == "human_review"
    assert "disabled" in decision["reason"]
    assert len(json.loads(decision["tool_trace_ids_json"])) == 1
    assert decision["input_tokens"] == 16
    assert decision["output_tokens"] == 8
    assert decision["total_tokens"] == 24
    assert decision["cost_usd"] > 0
    assert decision["model_slug"] == client.config.canonical_slug


def test_same_fake_llm_input_replays_identical_decision(p1_2_store) -> None:
    responses = [
        {"sql": "SELECT COUNT(*) AS n FROM asof.transactions"},
        {
            "cause": "other",
            "runbook": None,
            "action": "human_review",
            "reason": "Need an operator to inspect this incident.",
        },
    ]
    first = _gate(
        _agent(p1_2_store, FakeLLMClient(responses=responses)).propose(
            INCIDENT,
            as_of_time=AS_OF,
            checklist_results={},
        )
    )
    second = _gate(
        _agent(p1_2_store, FakeLLMClient(responses=responses)).propose(
            INCIDENT,
            as_of_time=AS_OF,
            checklist_results={},
        )
    )

    assert first == second


@pytest.mark.parametrize(
    ("proposal_response", "error_code"),
    [
        ("not-json", "invalid_json"),
        (
            {
                "model": "qwen/qwen3.7-flash",
                "choices": [
                    {"message": {"content": ""}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 8, "completion_tokens": 0},
            },
            "empty_content",
        ),
        (
            {
                "model": "qwen/qwen3.7-flash",
                "choices": [
                    {"message": {"content": "{}"}, "finish_reason": "length"}
                ],
                "usage": {"prompt_tokens": 8, "completion_tokens": 4},
            },
            "token_limit",
        ),
    ],
)
def test_invalid_or_incomplete_proposal_routes_to_human_review(
    p1_2_store,
    proposal_response,
    error_code: str,
) -> None:
    client = FakeLLMClient(
        responses=[{"sql": "SELECT 1"}, proposal_response],
    )
    result = _agent(p1_2_store, client).propose(
        INCIDENT,
        as_of_time=AS_OF,
        checklist_results={},
    )

    decision = _gate(result)
    assert decision["action"] == "human_review"
    assert error_code in decision["reason"]
    assert decision["evidence_status"] == "insufficient"
    assert len(json.loads(decision["tool_trace_ids_json"])) == 1


def test_sql_failure_routes_to_human_review_and_keeps_trace(p1_2_store) -> None:
    client = FakeLLMClient(
        responses=[
            {"sql": "SELECT * FROM main.transactions"},
            {
                "cause": "fraud_surge",
                "runbook": None,
                "action": "fraud_ops",
                "reason": "Please investigate the potential fraud signal.",
            },
        ],
    )
    result = _agent(p1_2_store, client).propose(
        INCIDENT,
        as_of_time=AS_OF,
        checklist_results={},
    )

    decision = _gate(result)
    assert len(client.requests) == 2
    assert decision["action"] == "human_review"
    assert "sql_execution_error" in decision["reason"]
    assert decision["evidence_status"] == "insufficient"
    assert len(json.loads(decision["tool_trace_ids_json"])) == 1


def test_cost_limit_routes_to_human_review(p1_2_store) -> None:
    config = replace(load_llm_config(), max_total_cost_usd=0.0000005)
    client = FakeLLMClient(
        responses=[
            {"sql": "SELECT 1"},
            {
                "cause": "other",
                "runbook": None,
                "action": "human_review",
                "reason": "Review is required when the budget is exhausted.",
            },
        ],
        config=config,
    )
    result = _agent(p1_2_store, client).propose(
        INCIDENT,
        as_of_time=AS_OF,
        checklist_results={},
    )

    decision = _gate(result)
    assert decision["action"] == "human_review"
    assert "cost_limit_exceeded" in decision["reason"]
    assert decision["cost_usd"] > config.max_total_cost_usd
