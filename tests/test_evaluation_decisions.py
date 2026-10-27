from __future__ import annotations

import ast
import json
from pathlib import Path

from evaluation import evaluate_decisions
from simulator.storage import create_export_stores, insert_private_record


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
    effect_end_at: str | None = "2026-02-10T00:00:00.000000Z",
    requires_intervention: bool = True,
    accepted_actions: list[str] | None = None,
    features: list[str] | None = None,
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
            "intervention_start_at": effect_available_at,
            "deadline_at": "2026-02-12T00:00:00.000000Z",
            "accepted_actions_json": (
                accepted_actions if accepted_actions is not None else ["fraud_ops", "human_review"]
            ),
            "parent_scenario": None,
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
                    feature="unknown_feature",
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
