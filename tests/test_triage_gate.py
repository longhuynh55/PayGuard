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


@pytest.mark.parametrize("source", ["rule", "agent"])
@pytest.mark.parametrize(
    ("cause", "runbook"),
    [
        ("legit_business_change", "unit_or_null_change"),
        ("seasonal", "unit_or_null_change"),
        ("seasonal", "small_sample"),
        ("seasonal", "segment_concentration"),
        ("seasonal", "invented_benign_runbook"),
        ("legit_business_change", "seasonal_calendar"),
        ("no_material_change", "seasonal_calendar"),
    ],
)
def test_review_r1_only_seasonal_cause_and_runbook_may_close(
    source: str, cause: str, runbook: str
) -> None:
    result = _gate(
        _proposal(
            "close",
            cause=cause,
            runbook=runbook,
            checklist_results={runbook: {"status": "pass"}},
        ),
        source=source,
        close_score=1.0 if source == "agent" else None,
        config=_config(
            close_enabled={runbook: True},
            agent_close_disabled=False,
            tau_agent=1.0,
        ),
    )

    assert result["action"] == "human_review"
    assert "seasonal cause" in result["reason"]


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
    assert result["evidence_status"] == "insufficient"


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
    assert result["evidence_status"] == "sufficient"
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


def test_data_ticket_required_checklist_failure_is_insufficient() -> None:
    result = _gate(
        _proposal(
            "data_ticket",
            cause="data_pipeline",
            runbook="unit_or_null_change",
            checklist_results={"unit_or_null_change": {"status": "fail"}},
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


@pytest.mark.parametrize("source", ["rule", "agent"])
def test_review_r2_seasonal_close_escalates_for_same_segment_fraud_signal(
    source: str,
) -> None:
    result = _gate(
        _proposal(
            "close",
            checklist_results={
                "seasonal_calendar": {"status": "pass"},
                "fraud_signal": {
                    "status": "pass",
                    "scope_match": True,
                    "window_complete": True,
                },
            },
        ),
        source=source,
        close_score=1.0 if source == "agent" else None,
        config=_config(
            close_enabled={"seasonal_calendar": True},
            agent_close_disabled=False,
            tau_agent=1.0,
        ),
    )

    assert result["action"] == "human_review"
    assert "same segment" in result["reason"]


@pytest.mark.parametrize("source", ["rule", "agent"])
@pytest.mark.parametrize("runbook", ["seasonal_calendar", "unit_or_null_change"])
def test_review_r2_runbook_escalation_precedes_other_action_branches(
    source: str, runbook: str
) -> None:
    result = _gate(
        _proposal(
            "fraud_ops",
            cause="fraud_surge",
            runbook=None,
            checklist_results={
                runbook: {"status": "pass", "scope_match": True},
                "fraud_signal": {
                    "status": "pass",
                    "scope_match": True,
                    "window_complete": True,
                }
            },
        ),
        source=source,
        config=_config(agent_close_disabled=False, tau_agent=1.0),
    )

    assert result["action"] == "human_review"
    assert f"{runbook!r}" in result["reason"]


@pytest.mark.parametrize("source", ["rule", "agent"])
@pytest.mark.parametrize("runbook", ["seasonal_calendar", "unit_or_null_change"])
@pytest.mark.parametrize("status", ["fail", "not_applicable"])
def test_review_r2_fix_unselected_out_of_scope_runbook_does_not_block_fraud_ops(
    source: str, runbook: str, status: str
) -> None:
    result = _gate(
        _proposal(
            "fraud_ops",
            cause="fraud_surge",
            runbook=None,
            checklist_results={
                runbook: {"status": status, "scope_match": False},
                "fraud_signal": {
                    "status": "pass",
                    "scope_match": True,
                    "window_complete": True,
                },
            },
        ),
        source=source,
    )

    assert result["action"] == "fraud_ops"
    assert result["evidence_status"] == "sufficient"


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
    assert result["evidence_status"] == "insufficient"


@pytest.mark.parametrize(
    ("action", "runbook", "checklist_name"),
    [
        ("data_ticket", "unit_or_null_change", "unit_or_null_change"),
        ("fraud_ops", None, "fraud_signal"),
    ],
)
@pytest.mark.parametrize(
    "invalid_flag",
    ["scope_match", "window_complete"],
)
def test_review_r3_false_scope_or_window_never_routes(
    action: str, runbook: str | None, checklist_name: str, invalid_flag: str
) -> None:
    checklist = {"status": "pass", "scope_match": True, "window_complete": True}
    checklist[invalid_flag] = False
    result = _gate(
        _proposal(
            action,
            cause="data_pipeline" if action == "data_ticket" else "fraud_surge",
            runbook=runbook,
            checklist_results={checklist_name: checklist},
        )
    )

    assert result["action"] == "human_review"
    assert result["evidence_status"] == "insufficient"


@pytest.mark.parametrize("invalid_flag", ["scope_match", "window_complete"])
def test_review_r4_false_scope_or_window_never_closes(
    invalid_flag: str,
) -> None:
    checklist = {"status": "pass", "scope_match": True, "window_complete": True}
    checklist[invalid_flag] = False
    result = _gate(
        _proposal("close", checklist_results={"seasonal_calendar": checklist}),
        config=_config(close_enabled={"seasonal_calendar": True}),
    )

    assert result["action"] == "human_review"
    assert result["evidence_status"] == "insufficient"


def test_review_r4_only_incomplete_window_can_recheck_with_waiting_reason() -> None:
    window_pending = _gate(
        _proposal(
            "close",
            checklist_results={
                "seasonal_calendar": {
                    "status": "pass",
                    "window_complete": False,
                    "reason": "Wait for the seven-day window to close.",
                }
            },
        ),
        config=_config(close_enabled={"seasonal_calendar": True}),
    )
    scope_mismatch = _gate(
        _proposal(
            "close",
            checklist_results={
                "seasonal_calendar": {
                    "status": "pass",
                    "scope_match": False,
                    "window_complete": False,
                    "reason": "Wait for another day.",
                }
            },
        ),
        recheck_reason="Wait for another day.",
        config=_config(close_enabled={"seasonal_calendar": True}),
    )

    assert window_pending["action"] == "recheck"
    assert scope_mismatch["action"] == "human_review"


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
    checklists = json.loads(result["checklist_results_json"])
    assert checklists["__runner_guard"]["status"] == "not_applicable"
    assert checklists["__runner_guard"]["details"] == {
        "forced_human": True,
        "reason_codes": ["runner_tool_error"],
    }


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
    checklists = json.loads(result["checklist_results_json"])
    assert checklists["__runner_guard"]["details"]["reason_codes"] == [
        "invalid_snapshot"
    ]


@pytest.mark.parametrize(
    ("field_name", "bad_timestamp"),
    [
        ("opened_at", "not-a-timestamp"),
        ("opened_at", "2026-02-10T16:00:00"),
        ("event_at", "not-a-timestamp"),
        ("available_at", "2026-02-10T16:00:00"),
    ],
)
def test_review_r5_bad_snapshot_timestamp_returns_valid_human_decision(
    field_name: str, bad_timestamp: str
) -> None:
    result = _gate(
        _proposal("human_review"),
        incident=_incident(**{field_name: bad_timestamp}),
    )
    scope = json.loads(result["scope_json"])

    assert result["action"] == "human_review"
    assert result["evidence_status"] == "insufficient"
    assert scope == {
        "feature": None,
        "segment": None,
        "start_at": None,
        "end_at": _timestamp(),
    }
    assert field_name in result["reason"]
    assert validate_decision_row(result) == result


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


@pytest.mark.parametrize("source", ["rule", "agent"])
def test_third_recheck_is_routed_to_human_for_each_source(source: str) -> None:
    result = _gate(
        _proposal(
            "recheck",
            checklist_results={"wait": {"status": "pending"}},
            reason="Wait for the next complete observation.",
        ),
        incident=_incident(incident_id=f"incident-{source}"),
        source=source,
        prior_recheck_as_of_times=(
            _timestamp(_AS_OF - timedelta(days=2)),
            _timestamp(_AS_OF - timedelta(days=1)),
        ),
    )

    assert result["action"] == "human_review"
    assert result["evidence_status"] == "insufficient"
    assert "maximum of two rechecks" in result["reason"]


def test_recheck_requires_24_hours_between_as_of_cutoffs() -> None:
    too_soon = _gate(
        _proposal("recheck", reason="Wait for more evidence."),
        prior_recheck_as_of_times=(_timestamp(_AS_OF - timedelta(hours=23)),),
    )
    exactly_one_day = _gate(
        _proposal("recheck", reason="Wait for more evidence."),
        prior_recheck_as_of_times=(_timestamp(_AS_OF - timedelta(days=1)),),
    )
    same_cutoff = _gate(
        _proposal("recheck", reason="Wait for more evidence."),
        prior_recheck_as_of_times=(_timestamp(),),
    )

    assert too_soon["action"] == "human_review"
    assert "at least 24 hours" in too_soon["reason"]
    assert exactly_one_day["action"] == "recheck"
    assert same_cutoff["action"] == "human_review"


def test_child_incident_starts_a_fresh_recheck_counter() -> None:
    result = _gate(
        _proposal("recheck", reason="Wait for the next complete observation."),
        incident=_incident(
            incident_id="incident-child", parent_incident_id="incident-parent"
        ),
    )

    assert result["action"] == "recheck"


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


def test_review_r7_decision_id_is_replayable_and_changes_with_result() -> None:
    proposal = _proposal(
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
    )
    low_score = _gate(proposal, source="agent", close_score=0.2)
    high_score = _gate(proposal, source="agent", close_score=0.9)
    replayed_low_score = _gate(proposal, source="agent", close_score=0.2)

    assert low_score["decision_id"] != high_score["decision_id"]
    assert low_score == replayed_low_score


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
