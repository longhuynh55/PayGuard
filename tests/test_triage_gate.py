from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import pytest
import yaml

from triage.decisions import DECISION_COLUMNS, DecisionSchemaError, validate_decision_row
from triage.gate import GateConfig, evaluate_gate


_AS_OF = datetime(2026, 2, 10, 16, 59, 59, 999999, tzinfo=UTC)


def _timestamp(value: datetime = _AS_OF) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _incident(**updates: object) -> dict[str, object]:
    row: dict[str, object] = {
        "incident_id": "incident-a",
        "parent_incident_id": None,
        "episode_id": "ep-a",
        "alert_ids_json": '["alert-a"]',
        "feature": "amount_avg_7d",
        "segment": "all",
        "opened_at": _timestamp(_AS_OF - timedelta(days=1)),
        "status": "open",
        "event_at": _timestamp(),
        "available_at": _timestamp(),
    }
    row.update(updates)
    return row


def _proposal(
    action: str,
    *,
    cause: str = "seasonal",
    runbook: str | None = "seasonal_calendar",
    checklist_results: dict[str, object] | None = None,
    **extra: object,
) -> dict[str, object]:
    proposal: dict[str, object] = {
        "cause": cause,
        "action": action,
        "runbook": runbook,
        "checklist_results": checklist_results or {},
    }
    proposal.update(extra)
    return proposal


def _config(
    *,
    close_enabled: dict[str, bool] | None = None,
    agent_close_disabled: bool = True,
    tau_agent: float | None = None,
) -> GateConfig:
    return GateConfig(
        policy_version="test-policy-v1",
        close_enabled=close_enabled or {},
        agent_close_disabled=agent_close_disabled,
        tau_agent=tau_agent,
    )


def _gate(
    proposal: dict[str, object],
    *,
    incident: dict[str, object] | None = None,
    source: str = "rule",
    config: GateConfig | None = None,
    **kwargs: object,
) -> dict[str, object]:
    return evaluate_gate(
        incident or _incident(),
        proposal,
        source=source,
        expected_episode_id="ep-a",
        as_of_time=_timestamp(),
        config=config or _config(),
        **kwargs,
    )


def _pass(runbook: str = "seasonal_calendar") -> dict[str, object]:
    return {runbook: {"status": "pass", "reason": "Checklist passed."}}


def test_default_triage_config_has_no_round_one_close_authority() -> None:
    config_path = Path(__file__).parents[1] / "configs" / "triage.yaml"
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config = GateConfig.from_mapping(config_data)

    assert config.policy_version
    assert config.close_enabled
    assert not any(config.close_enabled.values())
    assert config.agent_close_disabled is True
    assert config.tau_agent is None
    assert config_data["provenance"]


def test_rule_closes_only_with_passing_benign_checklist_and_runbook_authority() -> None:
    result = _gate(
        _proposal("close", checklist_results=_pass()),
        config=_config(close_enabled={"seasonal_calendar": True}),
    )

    assert result["action"] == "close"
    assert result["evidence_status"] == "sufficient"
    assert result["decision_at"] == result["as_of_time"] == _timestamp()
    assert result["close_score"] is None
    assert tuple(result) == DECISION_COLUMNS
    assert validate_decision_row(result) == result


def test_rule_cannot_close_when_runbook_is_disabled() -> None:
    result = _gate(_proposal("close", checklist_results=_pass()))

    assert result["action"] == "human_review"
    assert "not enabled" in result["reason"]


def test_agent_can_close_with_tau_even_when_rule_runbook_close_is_disabled() -> None:
    result = _gate(
        _proposal("close", checklist_results=_pass()),
        source="agent",
        close_score=1.0,
        config=_config(agent_close_disabled=False, tau_agent=2 / 3),
    )

    assert result["action"] == "close"
    assert result["close_score"] == 1.0


def test_agent_close_is_disabled_by_round_one_defaults() -> None:
    result = _gate(
        _proposal("close", checklist_results=_pass()),
        source="agent",
        close_score=1.0,
        config=_config(agent_close_disabled=True, tau_agent=1.0),
    )

    assert result["action"] == "human_review"
    assert "disabled" in result["reason"]


def test_agent_below_tau_rechecks_only_with_a_recorded_waiting_reason() -> None:
    result = _gate(
        _proposal("close", checklist_results=_pass()),
        source="agent",
        close_score=2 / 3 - 0.01,
        recheck_reason="Wait for another complete observation day.",
        config=_config(agent_close_disabled=False, tau_agent=2 / 3),
    )

    assert result["action"] == "recheck"
    assert "another complete observation day" in result["reason"]


def test_agent_below_tau_without_waiting_reason_goes_to_human() -> None:
    result = _gate(
        _proposal("close", checklist_results=_pass()),
        source="agent",
        close_score=0.5,
        config=_config(agent_close_disabled=False, tau_agent=2 / 3),
    )

    assert result["action"] == "human_review"


def test_pending_benign_checklist_rechecks_when_pending_reason_is_recorded() -> None:
    result = _gate(
        _proposal(
            "close",
            checklist_results={
                "seasonal_calendar": {
                    "status": "pending",
                    "reason": "Wait for the seven-day window to close.",
                }
            },
        ),
        config=_config(close_enabled={"seasonal_calendar": True}),
    )

    assert result["action"] == "recheck"
    assert result["evidence_status"] == "insufficient"


def test_close_without_passed_benign_checklist_or_recheck_reason_goes_to_human() -> None:
    result = _gate(
        _proposal("close", checklist_results={"seasonal_calendar": {"status": "fail"}}),
        config=_config(close_enabled={"seasonal_calendar": True}),
    )

    assert result["action"] == "human_review"
    assert result["evidence_status"] == "sufficient"


def test_data_ticket_routes_without_benign_evidence_or_close_permissions() -> None:
    result = _gate(
        _proposal(
            "data_ticket",
            cause="data_pipeline",
            runbook="unit_or_null_change",
            checklist_results={
                "unit_or_null_change": {"status": "pass"},
                "seasonal_calendar": {"status": "fail"},
            },
        ),
        config=_config(),
    )

    assert result["action"] == "data_ticket"
    assert result["close_score"] is None


def test_data_ticket_requires_its_routing_checklist() -> None:
    result = _gate(
        _proposal(
            "data_ticket",
            cause="data_pipeline",
            runbook="unit_or_null_change",
            checklist_results={"unit_or_null_change": {"status": "pending"}},
        )
    )

    assert result["action"] == "human_review"
    assert result["evidence_status"] == "insufficient"


def test_data_ticket_escalates_when_fraud_signal_may_overlap() -> None:
    result = _gate(
        _proposal(
            "data_ticket",
            cause="data_pipeline",
            runbook="unit_or_null_change",
            checklist_results={
                "unit_or_null_change": {"status": "pass"},
                "fraud_signal": {"status": "pass", "scope_match": True},
            },
        )
    )

    assert result["action"] == "human_review"
    assert "fraud signal" in result["reason"]


def test_fraud_ops_routes_with_structured_in_scope_signal_and_complete_window() -> None:
    result = _gate(
        _proposal(
            "fraud_ops",
            cause="fraud_surge",
            runbook=None,
            checklist_results={
                "fraud_signal": {
                    "status": "pass",
                    "scope_match": True,
                    "window_complete": True,
                }
            },
        ),
        config=_config(),
    )

    assert result["action"] == "fraud_ops"


@pytest.mark.parametrize(
    "fraud_result",
    [
        {"status": "fail", "scope_match": True, "window_complete": True},
        {"status": "pass", "scope_match": False, "window_complete": True},
        {"status": "pass", "scope_match": True, "window_complete": False},
        {"status": "pending", "scope_match": True, "window_complete": True},
    ],
)
def test_fraud_ops_requires_signal_scope_and_complete_window(
    fraud_result: dict[str, object],
) -> None:
    result = _gate(
        _proposal(
            "fraud_ops",
            cause="fraud_surge",
            runbook=None,
            checklist_results={"fraud_signal": fraud_result},
        )
    )

    assert result["action"] == "human_review"


@pytest.mark.parametrize("action", ["close", "recheck", "data_ticket", "fraud_ops", "human_review"])
def test_mandatory_human_review_flag_wins_every_action_branch(action: str) -> None:
    if action == "data_ticket":
        proposal = _proposal(
            action,
            cause="data_pipeline",
            runbook="unit_or_null_change",
            checklist_results={"unit_or_null_change": {"status": "pass"}},
        )
    elif action == "fraud_ops":
        proposal = _proposal(
            action,
            cause="fraud_surge",
            runbook=None,
            checklist_results={
                "fraud_signal": {
                    "status": "pass",
                    "scope_match": True,
                    "window_complete": True,
                }
            },
        )
    else:
        proposal = _proposal(action, checklist_results=_pass(), reason="Review requested.")

    result = _gate(
        proposal,
        mandatory_human_review=True,
        mandatory_review_reasons=["A mandatory runbook condition fired."],
        config=_config(close_enabled={"seasonal_calendar": True}),
    )

    assert result["action"] == "human_review"
    assert "mandatory runbook condition" in result["reason"]


def test_checklist_human_review_condition_wins_without_a_separate_flag() -> None:
    result = _gate(
        _proposal(
            "data_ticket",
            cause="data_pipeline",
            runbook="unit_or_null_change",
            checklist_results={
                "unit_or_null_change": {
                    "status": "pass",
                    "requires_human_review": True,
                    "reason": "Fraud evidence conflicts with the pipeline change.",
                }
            },
        )
    )

    assert result["action"] == "human_review"


def test_tool_error_forces_human_review_even_when_routing_evidence_passes() -> None:
    result = _gate(
        _proposal(
            "data_ticket",
            cause="data_pipeline",
            runbook="unit_or_null_change",
            checklist_results={"unit_or_null_change": {"status": "pass"}},
        ),
        tool_error=True,
        tool_error_reason="Operational database read failed.",
    )

    assert result["action"] == "human_review"
    assert result["evidence_status"] == "insufficient"
    assert "database read failed" in result["reason"]


def test_checklist_tool_error_forces_human_instead_of_recheck() -> None:
    result = _gate(
        _proposal(
            "recheck",
            checklist_results={
                "seasonal_calendar": {"status": "error", "reason": "Tool timed out."}
            },
            reason="Wait for more evidence.",
        )
    )

    assert result["action"] == "human_review"
    assert result["evidence_status"] == "insufficient"


@pytest.mark.parametrize(
    ("incident", "expected_episode_id"),
    [
        (_incident(episode_id="ep-other"), "ep-a"),
        (
            _incident(available_at=_timestamp(_AS_OF + timedelta(seconds=1))),
            "ep-a",
        ),
        (_incident(extra_column="invalid"), "ep-a"),
    ],
)
def test_invalid_asof_episode_or_snapshot_schema_forces_human(
    incident: dict[str, object], expected_episode_id: str
) -> None:
    result = evaluate_gate(
        incident,
        _proposal(
            "data_ticket",
            cause="data_pipeline",
            runbook="unit_or_null_change",
            checklist_results={"unit_or_null_change": {"status": "pass"}},
        ),
        source="rule",
        expected_episode_id=expected_episode_id,
        as_of_time=_timestamp(),
        config=_config(),
    )

    assert result["action"] == "human_review"


def test_conflicting_evidence_is_computed_and_forces_human_review() -> None:
    result = _gate(
        _proposal(
            "fraud_ops",
            cause="fraud_surge",
            runbook=None,
            checklist_results={"fraud_signal": {"status": "conflicting"}},
        )
    )

    assert result["action"] == "human_review"
    assert result["evidence_status"] == "conflicting"


def test_agent_cannot_supply_evidence_status() -> None:
    result = _gate(
        _proposal(
            "close",
            checklist_results={"seasonal_calendar": {"status": "pending"}},
            evidence_status="sufficient",
        ),
        source="agent",
        config=_config(agent_close_disabled=False, tau_agent=2 / 3),
        close_score=1.0,
    )

    assert result["evidence_status"] == "insufficient"
    assert result["action"] == "human_review"
    assert "evidence_status" not in json.loads(result["checklist_results_json"])["seasonal_calendar"]


def test_direct_recheck_requires_a_reason() -> None:
    no_reason = _gate(_proposal("recheck", checklist_results={"wait": {"status": "pending"}}))
    with_reason = _gate(
        _proposal("recheck", checklist_results={"wait": {"status": "pending"}}),
        recheck_reason="Wait for the delayed batch to arrive.",
    )

    assert no_reason["action"] == "human_review"
    assert with_reason["action"] == "recheck"


def test_human_review_needs_neither_benign_evidence_nor_close_score() -> None:
    result = _gate(
        _proposal("human_review", cause="new_fraud", runbook=None), source="agent"
    )

    assert result["action"] == "human_review"
    assert result["close_score"] is None


def test_decision_row_schema_rejects_unknown_columns() -> None:
    result = _gate(_proposal("human_review"))
    result["truth"] = "must not be stored"

    with pytest.raises(DecisionSchemaError, match="columns do not match"):
        validate_decision_row(result)


def test_decision_row_schema_rejects_nested_agent_evidence_status() -> None:
    result = _gate(_proposal("human_review", checklist_results=_pass()))
    checklist_results = json.loads(result["checklist_results_json"])
    checklist_results["seasonal_calendar"]["evidence_status"] = "sufficient"
    result["checklist_results_json"] = json.dumps(checklist_results)

    with pytest.raises(DecisionSchemaError, match="unsupported fields"):
        validate_decision_row(result)


def test_rule_decisions_keep_agent_score_null_and_record_usage() -> None:
    result = _gate(
        _proposal("data_ticket", cause="data_pipeline", runbook="unit_or_null_change", checklist_results={
            "unit_or_null_change": {"status": "pass"}
        }),
        usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "cost_usd": 0.001},
    )

    assert result["close_score"] is None
    assert result["input_tokens"] == 10
    assert result["output_tokens"] == 5
    assert result["total_tokens"] == 15
    assert result["cost_usd"] == 0.001
