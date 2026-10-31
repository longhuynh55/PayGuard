from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from evaluation import DecisionEvaluationError, evaluate_decisions, evaluate_operational_decisions
from evaluation.__main__ import main as evaluation_main
from simulator.storage import (
    create_export_stores,
    insert_operational_record,
    insert_private_record,
)
from triage.decisions import persist_decisions
from triage.gate import GateConfig, evaluate_gate


ROOT = Path(__file__).resolve().parents[1]
EPISODE_ID = "ep-decision-eval"


def _manifest_config() -> dict[str, object]:
    return {
        "simulator_config": {
            "timeline": {
                "t0_business_date": {"value": "2026-02-01"},
                "business_timezone": {"value": "Asia/Ho_Chi_Minh"},
            }
        },
        "episode_config": {"protocol": {"duration_days": {"value": 30}}},
    }


def _add_episode(store) -> None:
    insert_operational_record(
        store.operational,
        "episodes",
        {"episode_id": EPISODE_ID, "role": "calibration"},
    )
    insert_private_record(store.private, "private_episodes", {"episode_id": EPISODE_ID})
    insert_private_record(
        store.private,
        "episode_manifest",
        {
            "episode_id": EPISODE_ID,
            "role": "calibration",
            "seed": 123,
            "template": "data_pipeline",
            "content_fingerprint": "a" * 64,
            "config_fingerprint": "b" * 64,
            "config_json": json.dumps(_manifest_config(), sort_keys=True, separators=(",", ":")),
        },
    )


def _add_truth(
    store,
    *,
    truth_id: str = "truth-1",
    effect_start_at: str = "2026-02-05T00:00:00.000000Z",
    effect_available_at: str = "2026-02-05T00:00:00.000000Z",
    intervention_start_at: str | None = None,
    deadline_at: str = "2026-02-12T00:00:00.000000Z",
    effect_end_at: str | None = "2026-02-10T00:00:00.000000Z",
    requires_intervention: bool = True,
    accepted_actions: list[str] | None = None,
    features: list[str] | None = None,
    parent_scenario: str | None = None,
) -> None:
    insert_private_record(
        store.private,
        "incident_truth",
        {
            "incident_truth_id": truth_id,
            "episode_id": EPISODE_ID,
            "cause": "data_pipeline" if requires_intervention else "seasonal",
            "requires_intervention": int(requires_intervention),
            "mechanism": "test-fixture",
            "rule_covered": 1,
            "affected_entities_json": [],
            "affected_segments_json": ["retail"],
            "affected_features_json": features or ["amount"],
            "effect_start_at": effect_start_at,
            "effect_end_at": effect_end_at,
            "effect_available_at": effect_available_at,
            "intervention_start_at": intervention_start_at or effect_available_at,
            "deadline_at": deadline_at,
            "accepted_actions_json": (
                accepted_actions if accepted_actions is not None else ["fraud_ops", "human_review"]
            ),
            "parent_scenario": parent_scenario,
        },
    )


def _decision(
    decision_id: str,
    decision_at: str,
    action: str,
    *,
    incident_id: str = "incident-1",
    feature: str | None = "amount_avg_7d",
    segment: str | None = "retail",
    scope_start_at: str = "2026-02-04T00:00:00.000000Z",
) -> dict[str, object]:
    return {
        "decision_id": decision_id,
        "incident_id": incident_id,
        "episode_id": EPISODE_ID,
        "decision_at": decision_at,
        "as_of_time": decision_at,
        "event_at": decision_at,
        "available_at": decision_at,
        "source": "rule",
        "runbook": "unit_or_null_change",
        "cause": "data_pipeline",
        "evidence_status": "sufficient",
        "action": action,
        "scope_json": json.dumps(
            {
                "feature": feature,
                "segment": segment,
                "start_at": scope_start_at,
                "end_at": decision_at,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        "checklist_results_json": "{}",
        "tool_trace_ids_json": "[]",
        "conclusion": f"Fixture decision {decision_id}",
        "reason": "Hand-authored evaluation fixture",
        "close_score": None,
        "policy_version": "fixture-v1",
        "model_slug": None,
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "cost_usd": None,
    }


def _evaluate(store, decisions: list[dict[str, object]]):
    return evaluate_decisions(
        decisions,
        private_db_path=store.private_path,
        episode_ids=[EPISODE_ID],
    )


def _store(tmp_path: Path, *, include_truth: bool = True):
    store = create_export_stores(tmp_path / "evaluation-run")
    _add_episode(store)
    if include_truth:
        _add_truth(store)
    store.commit()
    return store


def test_close_before_effect_availability_is_not_penalized(tmp_path: Path) -> None:
    store = _store(
        tmp_path,
        include_truth=False,
    )
    try:
        _add_truth(
            store,
            effect_start_at="2026-02-08T00:00:00.000000Z",
            effect_available_at="2026-02-08T00:00:00.000000Z",
        )
        store.commit()
        result = _evaluate(
            store,
            [_decision("close-before-effect", "2026-02-06T00:00:00.000000Z", "close")],
        )

        assert result.report["decision_matches"][0]["status"] == "unmatched"
        assert result.report["unsafe_truth_rate"]["unsafe"] == 0
        assert result.report["dangerous_closure_rate"]["dangerous_closures"] == 0
        assert result.report["decision_counts"]["unmatched_closures"] == 1
        assert result.report["unsafe_truth_rate"]["lower_bound_observed"] is True
    finally:
        store.close()


def test_unsafe_close_stays_unsafe_after_child_routes_truth(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        result = _evaluate(
            store,
            [
                _decision("unsafe-close", "2026-02-06T00:00:00.000000Z", "close"),
                _decision(
                    "child-route",
                    "2026-02-09T00:00:00.000000Z",
                    "fraud_ops",
                    incident_id="incident-child",
                ),
            ],
        )

        assert result.report["unsafe_truth_rate"]["unsafe_truth_ids"] == ["truth-1"]
        assert result.report["unsafe_truth_rate"]["rate"] == 1.0
        assert result.report["timely_routing_recall"]["truth_ids"] == ["truth-1"]
        assert result.report["dangerous_closure_rate"]["dangerous_incident_ids"] == [
            "incident-1"
        ]
        assert result.report["dangerous_closure_rate"]["denominator"] == 2
    finally:
        store.close()


def test_multiple_unsafe_closures_count_truth_once(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        result = _evaluate(
            store,
            [
                _decision("unsafe-close-1", "2026-02-06T00:00:00.000000Z", "close"),
                _decision(
                    "unsafe-close-2",
                    "2026-02-07T00:00:00.000000Z",
                    "close",
                    incident_id="incident-child",
                ),
            ],
        )

        assert result.report["unsafe_truth_rate"]["unsafe"] == 1
        assert result.report["unsafe_truth_rate"]["denominator"] == 1
        assert result.report["dangerous_closure_rate"]["dangerous_closures"] == 2
        assert result.report["dangerous_closure_rate"]["denominator"] == 2
    finally:
        store.close()


def test_close_never_counts_as_routing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        result = _evaluate(
            store,
            [_decision("close-only", "2026-02-06T00:00:00.000000Z", "close")],
        )

        routing = result.report["timely_routing_recall"]
        assert routing["denominator"] == 1
        assert routing["routed"] == 0
        assert routing["recall"] == 0.0
        assert routing["automatic_routing"]["routed"] == 0
        assert routing["human_review"]["routed"] == 0
    finally:
        store.close()


def test_routing_before_intervention_or_after_deadline_is_not_timely(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        result = _evaluate(
            store,
            [
                _decision("too-early", "2026-02-04T00:00:00.000000Z", "fraud_ops"),
                _decision(
                    "too-late",
                    "2026-02-13T00:00:00.000000Z",
                    "fraud_ops",
                    incident_id="incident-late",
                ),
            ],
        )

        routing = result.report["timely_routing_recall"]
        assert routing["truth_ids"] == []
        assert routing["recall"] == 0.0
        assert result.report["decision_matches"][0]["status"] == "unmatched"
        assert result.report["decision_matches"][1]["status"] == "matched"
    finally:
        store.close()


def test_routing_after_availability_before_intervention_is_matched_but_not_timely(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path, include_truth=False)
    try:
        _add_truth(
            store,
            effect_available_at="2026-02-05T00:00:00.000000Z",
            intervention_start_at="2026-02-08T00:00:00.000000Z",
            deadline_at="2026-02-15T00:00:00.000000Z",
        )
        store.commit()
        result = _evaluate(
            store,
            [_decision("routed-before-intervention", "2026-02-07T00:00:00.000000Z", "fraud_ops")],
        )

        routing = result.report["timely_routing_recall"]
        assert result.report["decision_matches"][0]["status"] == "matched"
        assert routing["routed"] == 0
        assert routing["missing_truth_ids"] == ["truth-1"]
    finally:
        store.close()


@pytest.mark.parametrize(
    ("decision_at", "routed"),
    [
        ("2026-02-08T00:00:00.000000Z", 1),
        ("2026-02-15T00:00:00.000000Z", 1),
        ("2026-02-15T00:00:00.000001Z", 0),
    ],
    ids=["at-intervention-start", "at-deadline", "one-microsecond-after-deadline"],
)
def test_routing_window_boundaries_are_inclusive(
    tmp_path: Path, decision_at: str, routed: int
) -> None:
    store = _store(tmp_path, include_truth=False)
    try:
        _add_truth(
            store,
            effect_available_at="2026-02-05T00:00:00.000000Z",
            intervention_start_at="2026-02-08T00:00:00.000000Z",
            deadline_at="2026-02-15T00:00:00.000000Z",
        )
        store.commit()
        result = _evaluate(store, [_decision("boundary-route", decision_at, "fraud_ops")])

        assert result.report["decision_matches"][0]["status"] == "matched"
        assert result.report["timely_routing_recall"]["routed"] == routed
    finally:
        store.close()


def test_zero_denominators_are_null(tmp_path: Path) -> None:
    store = _store(tmp_path, include_truth=False)
    try:
        result = _evaluate(store, [])

        assert result.report["timely_routing_recall"]["denominator"] == 0
        assert result.report["timely_routing_recall"]["recall"] is None
        assert result.report["unsafe_truth_rate"]["rate"] is None
        assert result.report["dangerous_closure_rate"]["rate"] is None
    finally:
        store.close()


def test_unmatched_close_is_not_treated_as_benign(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        result = _evaluate(
            store,
            [
                _decision(
                    "unmatched-close",
                    "2026-02-06T00:00:00.000000Z",
                    "close",
                    # Registered lookback, but the truth only affects amount.
                    feature="txn_count_24h",
                )
            ],
        )

        assert result.report["decision_matches"][0]["status"] == "unmatched"
        assert result.report["truth_counts"]["benign"] == 0
        assert result.report["decision_counts"]["unmatched_closures"] == 1
        assert result.report["unsafe_truth_rate"]["unsafe"] == 0
        assert result.report["unsafe_truth_rate"]["denominator"] == 1
        assert result.report["unsafe_truth_rate"]["lower_bound_observed"] is True
    finally:
        store.close()


def test_ambiguous_close_scores_all_dangerous_candidate_truths(tmp_path: Path) -> None:
    store = _store(tmp_path, include_truth=False)
    try:
        _add_truth(store, truth_id="truth-a")
        _add_truth(store, truth_id="truth-b")
        store.commit()

        # Per docs/evaluation.md section 2 and the 31/10 user decision, ambiguous
        # cause attribution does not suppress any candidate from risk metrics.
        result = _evaluate(
            store,
            [_decision("ambiguous-close", "2026-02-06T00:00:00.000000Z", "close")],
        )

        match = result.report["decision_matches"][0]
        assert match["status"] == "ambiguous_match"
        assert match["matched_truth_ids"] == []
        assert match["candidate_truth_ids"] == ["truth-a", "truth-b"]
        assert result.report["decision_counts"]["ambiguous"] == 1
        assert result.report["decision_counts"]["ambiguous_closures"] == 1
        assert result.report["unsafe_truth_rate"]["unsafe"] == 2
        assert result.report["unsafe_truth_rate"]["denominator"] == 2
        assert result.report["unsafe_truth_rate"]["unsafe_truth_ids"] == [
            "truth-a",
            "truth-b",
        ]
        assert result.report["unsafe_truth_rate"]["lower_bound_observed"] is False
        assert result.report["unsafe_truth_rate"]["basis"] == "all_candidate_truths_included"
        assert result.report["metric_window_days"] == 7
    finally:
        store.close()


def test_mixed_benign_and_fraud_candidates_make_close_unsafe_for_fraud(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path, include_truth=False)
    try:
        _add_truth(
            store,
            truth_id="B1",
            requires_intervention=False,
            accepted_actions=[],
            parent_scenario="mixed-scenario",
        )
        _add_truth(
            store,
            truth_id="F1",
            requires_intervention=True,
            parent_scenario="mixed-scenario",
        )
        store.commit()

        # Per docs/evaluation.md section 2 and the 31/10 user decision, risk
        # metrics include every candidate even when cause attribution is ambiguous.
        result = _evaluate(
            store,
            [_decision("mixed-close", "2026-02-06T00:00:00.000000Z", "close")],
        )

        match = result.report["decision_matches"][0]
        assert match["status"] == "ambiguous_match"
        assert match["candidate_truth_ids"] == ["B1", "F1"]
        assert result.report["unsafe_truth_rate"]["unsafe_truth_ids"] == ["F1"]
        assert result.report["unsafe_truth_rate"]["unsafe"] == 1
        assert result.report["unsafe_truth_rate"]["lower_bound_observed"] is False
    finally:
        store.close()


def test_delayed_benign_candidate_does_not_hide_new_fraud_close(tmp_path: Path) -> None:
    store = _store(tmp_path, include_truth=False)
    try:
        _add_truth(
            store,
            truth_id="B1-old",
            requires_intervention=False,
            accepted_actions=[],
            effect_start_at="2026-01-25T00:00:00.000000Z",
            effect_available_at="2026-01-25T00:00:00.000000Z",
            effect_end_at="2026-02-01T00:00:00.000000Z",
            parent_scenario="delayed-mixed-scenario",
        )
        _add_truth(
            store,
            truth_id="F1-new",
            effect_start_at="2026-02-10T00:00:00.000000Z",
            effect_available_at="2026-02-10T00:00:00.000000Z",
            effect_end_at="2026-02-11T00:00:00.000000Z",
            deadline_at="2026-02-17T00:00:00.000000Z",
            parent_scenario="delayed-mixed-scenario",
        )
        store.commit()

        result = _evaluate(
            store,
            [_decision("delayed-mixed-close", "2026-02-11T00:00:00.000000Z", "close")],
        )

        match = result.report["decision_matches"][0]
        assert match["status"] == "ambiguous_match"
        assert match["candidate_truth_ids"] == ["B1-old", "F1-new"]
        assert result.report["unsafe_truth_rate"]["unsafe_truth_ids"] == ["F1-new"]
    finally:
        store.close()


def test_human_review_routes_both_same_feature_cause_components(tmp_path: Path) -> None:
    store = _store(tmp_path, include_truth=False)
    try:
        _add_truth(
            store,
            truth_id="pipeline-component",
            parent_scenario="two-cause-scenario",
        )
        _add_truth(
            store,
            truth_id="fraud-component",
            parent_scenario="two-cause-scenario",
        )
        store.commit()

        result = _evaluate(
            store,
            [_decision("two-cause-human-review", "2026-02-06T00:00:00.000000Z", "human_review")],
        )

        assert result.report["decision_matches"][0]["status"] == "ambiguous_match"
        assert result.report["timely_routing_recall"]["truth_ids"] == [
            "fraud-component",
            "pipeline-component",
        ]
        assert result.report["timely_routing_recall"]["human_review"]["routed"] == 2
        assert result.report["timely_routing_recall"]["recall"] == 1.0
    finally:
        store.close()


def test_auto_routing_and_human_review_are_reported_separately(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        result = _evaluate(
            store,
            [
                _decision("auto-route", "2026-02-06T00:00:00.000000Z", "fraud_ops"),
                _decision(
                    "safe-route",
                    "2026-02-07T00:00:00.000000Z",
                    "human_review",
                    incident_id="incident-review",
                ),
            ],
        )

        routing = result.report["timely_routing_recall"]
        assert routing["truth_ids"] == ["truth-1"]
        assert routing["automatic_routing"]["truth_ids"] == ["truth-1"]
        assert routing["human_review"]["truth_ids"] == ["truth-1"]
        assert routing["automatic_routing"]["recall"] == 1.0
        assert routing["human_review"]["recall"] == 1.0
        assert result.report_path.is_file()
        assert result.report_path.is_relative_to(store.private_path.parent)
        assert not result.report_path.is_relative_to(store.operational_path.parent / "reports")
        assert json.loads(result.report_path.read_text(encoding="utf-8")) == result.report
    finally:
        store.close()


def test_operational_evaluator_reads_persisted_decisions_and_writes_private_report(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        decision_at = "2026-02-09T00:00:00.000000Z"
        incident = {
            "incident_id": "incident-1",
            "parent_incident_id": None,
            "episode_id": EPISODE_ID,
            "alert_ids_json": '["alert-1"]',
            "feature": "amount_avg_7d",
            "segment": "retail",
            "opened_at": "2026-02-04T00:00:00.000000Z",
            "status": "open",
            "event_at": decision_at,
            "available_at": decision_at,
        }
        decision = evaluate_gate(
            incident,
            {
                "cause": "data_pipeline",
                "runbook": "unit_or_null_change",
                "action": "human_review",
                "checklist_results": {},
                "reason": "Operational database integration fixture.",
            },
            source="rule",
            expected_episode_id=EPISODE_ID,
            as_of_time=decision_at,
            config=GateConfig(policy_version="fixture-v1", close_enabled={}),
        )
        assert persist_decisions(store.operational, [decision]) == 1
        store.commit()

        result = evaluate_operational_decisions(
            store.operational_path,
            private_db_path=store.private_path,
        )

        assert result.report["decision_counts"]["total"] == 1
        assert result.report["timely_routing_recall"]["routed"] == 1
        assert result.report_path.is_relative_to(store.private_path.parent)
        assert json.loads(result.report_path.read_text(encoding="utf-8")) == result.report
    finally:
        store.close()


def test_data_ticket_is_automatic_routing_when_truth_accepts_it(tmp_path: Path) -> None:
    store = _store(tmp_path, include_truth=False)
    try:
        _add_truth(store, accepted_actions=["data_ticket", "human_review"])
        store.commit()
        result = _evaluate(
            store,
            [_decision("pipeline-ticket", "2026-02-06T00:00:00.000000Z", "data_ticket")],
        )

        routing = result.report["timely_routing_recall"]
        assert routing["truth_ids"] == ["truth-1"]
        assert routing["automatic_routing"]["truth_ids"] == ["truth-1"]
        assert routing["human_review"]["truth_ids"] == []
    finally:
        store.close()


def test_triage_runtime_modules_do_not_import_evaluation() -> None:
    violations = []
    for path in (ROOT / "triage").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                continue
            if any(name == "evaluation" or name.startswith("evaluation.") for name in modules):
                violations.append(str(path.relative_to(ROOT)))

    assert violations == []


def _agent_decision(
    decision_id: str,
    decision_at: str,
    action: str,
    *,
    incident_id: str,
    policy_version: str,
    model_slug: str,
) -> dict[str, object]:
    return {
        **_decision(decision_id, decision_at, action, incident_id=incident_id),
        "source": "agent",
        "policy_version": policy_version,
        "model_slug": model_slug,
        "tool_trace_ids_json": json.dumps([f"trace-{decision_id}"]),
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "cost_usd": 0.00001,
    }


def _decision_id(tag: str) -> str:
    """Map a readable tag onto the decision-<24 lowercase hex> ID that the DB CHECK requires."""
    return "decision-" + tag.encode("ascii").hex().rjust(24, "0")


def _persist_operational(store, rows: list[dict[str, object]]) -> None:
    persisted = [{**row, "decision_id": _decision_id(str(row["decision_id"]))} for row in rows]
    assert persist_decisions(store.operational, persisted) == len(persisted)
    store.commit()


def _persist_mixed_pairs(store) -> None:
    """Two agent models under fixture-v1, plus rule decisions under fixture-v1 and fixture-v2."""
    _persist_operational(
        store,
        [
            _agent_decision(
                "a-1",
                "2026-02-06T00:00:00.000000Z",
                "fraud_ops",
                incident_id="incident-1",
                policy_version="fixture-v1",
                model_slug="fake/a",
            ),
            _agent_decision(
                "a-2",
                "2026-02-07T00:00:00.000000Z",
                "human_review",
                incident_id="incident-2",
                policy_version="fixture-v1",
                model_slug="fake/a",
            ),
            _agent_decision(
                "b-1",
                "2026-02-06T00:00:00.000000Z",
                "close",
                incident_id="incident-3",
                policy_version="fixture-v1",
                model_slug="fake/b",
            ),
            _decision(
                "rule-1",
                "2026-02-06T00:00:00.000000Z",
                "human_review",
                incident_id="incident-4",
            ),
            {
                **_decision(
                    "rule-2",
                    "2026-02-06T00:00:00.000000Z",
                    "human_review",
                    incident_id="incident-5",
                ),
                "policy_version": "fixture-v2",
            },
        ],
    )


def test_mixed_agent_pairs_are_refused_without_selector(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _store(tmp_path)
    try:
        _persist_mixed_pairs(store)

        with pytest.raises(DecisionEvaluationError, match="2 .*pairs") as refused:
            evaluate_operational_decisions(
                store.operational_path,
                private_db_path=store.private_path,
            )
        assert "model_slug='fake/a'" in str(refused.value)
        assert "model_slug='fake/b'" in str(refused.value)

        assert evaluation_main(["decisions", "--run", str(tmp_path / "evaluation-run")]) == 2
        assert "ERROR: DecisionEvaluationError" in capsys.readouterr().err
        assert not (store.private_path.parent / "reports" / "p4_5_decisions.json").exists()
    finally:
        store.close()


@pytest.mark.parametrize(
    ("selector", "expected_ids", "expected_selection", "unsafe"),
    [
        (
            {"model_slug": "fake/a"},
            ["a-1", "a-2", "rule-1"],
            {"policy_version": "fixture-v1", "model_slug": "fake/a", "excluded_decision_count": 2},
            0,
        ),
        (
            {"policy_version": "fixture-v1", "model_slug": "fake/b"},
            ["b-1", "rule-1"],
            {"policy_version": "fixture-v1", "model_slug": "fake/b", "excluded_decision_count": 3},
            1,
        ),
    ],
    ids=["model-a", "model-b"],
)
def test_selector_picks_one_pair_and_counts_match(
    tmp_path: Path,
    selector: dict[str, str],
    expected_ids: list[str],
    expected_selection: dict[str, object],
    unsafe: int,
) -> None:
    store = _store(tmp_path)
    try:
        _persist_mixed_pairs(store)

        result = evaluate_operational_decisions(
            store.operational_path,
            private_db_path=store.private_path,
            **selector,
        )

        assert result.report["selection"] == expected_selection
        assert {match["decision_id"] for match in result.report["decision_matches"]} == {
            _decision_id(tag) for tag in expected_ids
        }
        assert result.report["decision_counts"]["total"] == len(expected_ids)
        assert result.report["unsafe_truth_rate"]["unsafe"] == unsafe
    finally:
        store.close()


@pytest.mark.parametrize(
    "selector",
    [
        {"policy_version": "fixture-v1"},
        {"model_slug": "fake/missing"},
        {"model_slug": " "},
    ],
    ids=["policy-matches-two-pairs", "no-matching-pair", "blank-selector"],
)
def test_selector_must_choose_exactly_one_pair(tmp_path: Path, selector: dict[str, str]) -> None:
    store = _store(tmp_path)
    try:
        _persist_mixed_pairs(store)

        with pytest.raises(DecisionEvaluationError):
            evaluate_operational_decisions(
                store.operational_path,
                private_db_path=store.private_path,
                **selector,
            )
    finally:
        store.close()


def test_cli_selector_flags_report_one_pair(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _store(tmp_path)
    try:
        _persist_mixed_pairs(store)

        exit_code = evaluation_main(
            ["decisions", "--run", str(tmp_path / "evaluation-run"), "--model-slug", "fake/b"]
        )

        assert exit_code == 0
        output = capsys.readouterr().out
        assert "decision_count=2" in output
        assert "policy_version=fixture-v1" in output
        assert "model_slug=fake/b" in output
        report = json.loads(
            (store.private_path.parent / "reports" / "p4_5_decisions.json").read_text(
                encoding="utf-8"
            )
        )
        assert report["selection"]["model_slug"] == "fake/b"
        assert report["decision_counts"]["total"] == 2
    finally:
        store.close()


def test_single_provider_db_is_reported_without_selector(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _store(tmp_path)
    try:
        _persist_operational(
            store,
            [
                _agent_decision(
                    "agent-1",
                    "2026-02-06T00:00:00.000000Z",
                    "fraud_ops",
                    incident_id="incident-1",
                    policy_version="fixture-v1",
                    model_slug="fake/a",
                ),
                _agent_decision(
                    "agent-2",
                    "2026-02-07T00:00:00.000000Z",
                    "human_review",
                    incident_id="incident-2",
                    policy_version="fixture-v1",
                    model_slug="fake/a",
                ),
                _decision(
                    "rule-1",
                    "2026-02-06T00:00:00.000000Z",
                    "data_ticket",
                    incident_id="incident-3",
                ),
            ],
        )

        result = evaluate_operational_decisions(
            store.operational_path,
            private_db_path=store.private_path,
        )

        assert result.report["selection"] == {
            "policy_version": "fixture-v1",
            "model_slug": "fake/a",
            "excluded_decision_count": 0,
        }
        assert result.report["decision_counts"]["total"] == 3
        assert evaluation_main(["decisions", "--run", str(tmp_path / "evaluation-run")]) == 0
        assert "decision_count=3" in capsys.readouterr().out
    finally:
        store.close()


def test_rule_only_db_with_several_policy_versions_needs_policy_selector(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        _persist_operational(
            store,
            [
                _decision(
                    "rule-1",
                    "2026-02-06T00:00:00.000000Z",
                    "data_ticket",
                    incident_id="incident-1",
                ),
                {
                    **_decision(
                        "rule-2",
                        "2026-02-06T00:00:00.000000Z",
                        "human_review",
                        incident_id="incident-2",
                    ),
                    "policy_version": "fixture-v2",
                },
            ],
        )

        with pytest.raises(DecisionEvaluationError, match="2 .*pairs"):
            evaluate_operational_decisions(
                store.operational_path,
                private_db_path=store.private_path,
            )

        result = evaluate_operational_decisions(
            store.operational_path,
            private_db_path=store.private_path,
            policy_version="fixture-v2",
        )
        assert result.report["selection"] == {
            "policy_version": "fixture-v2",
            "model_slug": None,
            "excluded_decision_count": 1,
        }
        assert result.report["decision_counts"]["total"] == 1
    finally:
        store.close()
