from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from evaluation import evaluate_detection_recall
from evaluation.matcher import EvaluationMatcherError
from monitoring.incidents import InMemoryDecisionSource, compute_incidents
from simulator.storage import create_export_stores, insert_private_record
from simulator.validate import RUN_METADATA_KEY


ROOT = Path(__file__).resolve().parents[1]
AS_OF = "2026-03-02T16:59:59.000000Z"


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


def _add_episode(store, episode_id: str, template: str) -> None:
    insert_private_record(
        store.private,
        "private_episodes",
        {"episode_id": episode_id},
    )
    config_json = json.dumps(_manifest_config(), sort_keys=True, separators=(",", ":"))
    insert_private_record(
        store.private,
        "episode_manifest",
        {
            "episode_id": episode_id,
            "role": "calibration",
            "seed": 123,
            "template": template,
            "content_fingerprint": "a" * 64,
            "config_fingerprint": "b" * 64,
            "config_json": config_json,
        },
    )


def _add_truth(
    store,
    *,
    episode_id: str,
    truth_id: str,
    cause: str,
    requires_intervention: bool,
    features: list[str],
    segments: list[str],
    effect_start_at: str = "2026-02-05T00:00:00.000000Z",
    effect_end_at: str | None = "2026-02-06T00:00:00.000000Z",
    effect_available_at: str = "2026-02-05T00:00:00.000000Z",
    parent_scenario: str | None = None,
) -> None:
    insert_private_record(
        store.private,
        "incident_truth",
        {
            "incident_truth_id": truth_id,
            "episode_id": episode_id,
            "cause": cause,
            "requires_intervention": int(requires_intervention),
            "mechanism": "test-fixture",
            "rule_covered": 1,
            "affected_entities_json": [],
            "affected_segments_json": segments,
            "affected_features_json": features,
            "effect_start_at": effect_start_at,
            "effect_end_at": effect_end_at,
            "effect_available_at": effect_available_at,
            "intervention_start_at": effect_available_at,
            "deadline_at": "2026-02-12T00:00:00.000000Z",
            "accepted_actions_json": ["human_review"] if requires_intervention else [],
            "parent_scenario": parent_scenario,
        },
    )


def _alert(
    *,
    episode_id: str,
    alert_id: str,
    feature: str,
    at: str,
    segment: str = "all",
) -> dict[str, object]:
    return {
        "alert_id": alert_id,
        "episode_id": episode_id,
        "as_of_time": at,
        "metric": f"psi.{feature}",
        "feature": feature,
        "segment": segment,
        "value": 0.5,
        "reference": "2025-11-01/2026-01-31",
        "threshold": 0.25,
        "event_at": at,
        "available_at": at,
    }


def _incident_rows(*alerts: dict[str, object], episode_id: str) -> tuple[dict[str, object], ...]:
    result = compute_incidents(list(alerts), episode_id=episode_id)
    return result.incidents


def _evaluate(store, incidents, episode_ids: list[str]):
    return evaluate_detection_recall(
        incidents,
        private_db_path=store.private_path,
        episode_ids=episode_ids,
        as_of_time=AS_OF,
    )


def test_matches_mapped_feature_and_marks_out_of_scope_incident_unmatched(tmp_path: Path) -> None:
    store = create_export_stores(tmp_path / "private-run")
    try:
        _add_episode(store, "ep-match", "data_pipeline")
        _add_truth(
            store,
            episode_id="ep-match",
            truth_id="truth-match",
            cause="data_pipeline",
            requires_intervention=True,
            features=["amount"],
            segments=["retail"],
        )
        store.commit()
        incidents = _incident_rows(
            _alert(
                episode_id="ep-match",
                alert_id="alert-match",
                feature="amount_avg_7d",
                at="2026-02-05T12:00:00.000000Z",
            ),
            _alert(
                episode_id="ep-match",
                alert_id="alert-unmatched",
                feature="amount_avg_7d",
                at="2026-02-05T13:00:00.000000Z",
                segment="card",
            ),
            _alert(
                episode_id="ep-match",
                alert_id="alert-wrong-feature",
                feature="txn_count_24h",
                at="2026-02-05T14:00:00.000000Z",
            ),
            episode_id="ep-match",
        )

        result = _evaluate(store, incidents, ["ep-match"])

        assert result.report["detection_recall"] == {
            "detected": 1,
            "denominator": 1,
            "recall": 1.0,
            "missed_truth_ids": [],
        }
        assert result.report["incident_counts"] == {
            "evaluated": 3,
            "matched": 1,
            "ambiguous": 0,
            "unmatched": 2,
            "benign_matched": 0,
        }
        assert len(result.report["unmatched_incident_ids"]) == 2
        assert result.report_path.is_file()
        assert result.report_path.is_relative_to(store.private_path.parent)
    finally:
        store.close()


def test_truth_with_no_alert_is_a_miss_in_recall_denominator(tmp_path: Path) -> None:
    store = create_export_stores(tmp_path / "no-alert-run")
    try:
        _add_episode(store, "ep-no-alert", "data_pipeline")
        _add_truth(
            store,
            episode_id="ep-no-alert",
            truth_id="truth-no-alert",
            cause="data_pipeline",
            requires_intervention=True,
            features=["amount"],
            segments=["retail"],
        )
        store.commit()

        result = _evaluate(store, (), ["ep-no-alert"])

        assert result.report["detection_recall"] == {
            "detected": 0,
            "denominator": 1,
            "recall": 0.0,
            "missed_truth_ids": ["truth-no-alert"],
        }
    finally:
        store.close()


def test_effect_available_at_is_the_earliest_matchable_truth_time(tmp_path: Path) -> None:
    store = create_export_stores(tmp_path / "effect-availability-run")
    try:
        _add_episode(store, "ep-effect-availability", "data_pipeline")
        _add_truth(
            store,
            episode_id="ep-effect-availability",
            truth_id="truth-effect-availability",
            cause="data_pipeline",
            requires_intervention=True,
            features=["amount"],
            segments=["retail"],
            effect_start_at="2026-02-05T00:00:00.000000Z",
            effect_available_at="2026-02-06T00:00:00.000000Z",
        )
        store.commit()
        incidents = _incident_rows(
            _alert(
                episode_id="ep-effect-availability",
                alert_id="alert-before-available",
                feature="amount_avg_7d",
                at="2026-02-05T12:00:00.000000Z",
            ),
            episode_id="ep-effect-availability",
        )

        result = _evaluate(store, incidents, ["ep-effect-availability"])

        assert result.report["detection_recall"]["recall"] == 0.0
        assert result.report["incident_counts"]["unmatched"] == 1
    finally:
        store.close()


def test_late_incident_inside_public_metric_window_matches_truth(tmp_path: Path) -> None:
    store = create_export_stores(tmp_path / "delayed-window-run")
    try:
        _add_episode(store, "ep-delayed-window", "data_pipeline")
        _add_truth(
            store,
            episode_id="ep-delayed-window",
            truth_id="truth-delayed-window",
            cause="data_pipeline",
            requires_intervention=True,
            features=["amount"],
            segments=["retail"],
        )
        store.commit()
        incidents = _incident_rows(
            _alert(
                episode_id="ep-delayed-window",
                alert_id="alert-delayed-window",
                feature="amount_avg_7d",
                at="2026-02-11T12:00:00.000000Z",
            ),
            episode_id="ep-delayed-window",
        )

        result = _evaluate(store, incidents, ["ep-delayed-window"])

        match = result.report["incident_matches"][0]
        assert result.report["metric_window_days"] == 7
        assert match["status"] == "matched"
        assert match["matched_truth_ids"] == ["truth-delayed-window"]
        assert match["candidate_truth_ids"] == ["truth-delayed-window"]
        assert result.report["detection_recall"]["recall"] == 1.0
    finally:
        store.close()


@pytest.mark.parametrize(
    ("feature", "truth_feature", "alert_at", "expected_status"),
    [
        ("amount_avg_7d", "amount", "2026-02-20T00:00:00.000000Z", "matched"),
        ("amount_avg_7d", "amount", "2026-02-20T00:00:00.000001Z", "unmatched"),
        ("txn_count_24h", "txn_count", "2026-02-14T00:00:00.000000Z", "matched"),
        ("txn_count_24h", "txn_count", "2026-02-14T00:00:00.000001Z", "unmatched"),
    ],
    ids=["amount-boundary", "amount-after-boundary", "count-boundary", "count-after-boundary"],
)
def test_metric_window_plus_feature_lookback_boundaries(
    tmp_path: Path,
    feature: str,
    truth_feature: str,
    alert_at: str,
    expected_status: str,
) -> None:
    store = create_export_stores(tmp_path / "feature-lookback-boundary-run")
    try:
        _add_episode(store, "ep-lookback-boundary", "data_pipeline")
        _add_truth(
            store,
            episode_id="ep-lookback-boundary",
            truth_id="truth-lookback-boundary",
            cause="data_pipeline",
            requires_intervention=True,
            features=[truth_feature],
            segments=["retail"],
        )
        store.commit()
        incidents = _incident_rows(
            _alert(
                episode_id="ep-lookback-boundary",
                alert_id="alert-lookback-boundary",
                feature=feature,
                at=alert_at,
            ),
            episode_id="ep-lookback-boundary",
        )

        result = _evaluate(store, incidents, ["ep-lookback-boundary"])

        assert result.report["metric_window_days"] == 7
        assert result.report["incident_matches"][0]["status"] == expected_status
    finally:
        store.close()


def test_run_resolved_metric_window_must_match_public_contract(tmp_path: Path) -> None:
    store = create_export_stores(tmp_path / "resolved-window-run")
    try:
        _add_episode(store, "ep-resolved-window", "data_pipeline")
        insert_private_record(
            store.private,
            "run_metadata",
            {
                "key": RUN_METADATA_KEY,
                "value_json": {
                    "resolved_config": {
                        "monitoring": {"window_days": {"value": 6}}
                    }
                },
            },
        )
        store.commit()

        with pytest.raises(EvaluationMatcherError, match="does not match"):
            _evaluate(store, (), ["ep-resolved-window"])
    finally:
        store.close()


def test_matching_run_resolved_metric_window_is_used(tmp_path: Path) -> None:
    store = create_export_stores(tmp_path / "resolved-window-match-run")
    try:
        _add_episode(store, "ep-resolved-window-match", "data_pipeline")
        insert_private_record(
            store.private,
            "run_metadata",
            {
                "key": RUN_METADATA_KEY,
                "value_json": {
                    "resolved_config": {
                        "monitoring": {"window_days": {"value": 7}}
                    }
                },
            },
        )
        store.commit()

        result = _evaluate(store, (), ["ep-resolved-window-match"])

        assert result.report["metric_window_days"] == 7
    finally:
        store.close()


def test_multiple_truth_candidates_are_ambiguous_and_counted_for_recall(
    tmp_path: Path,
) -> None:
    store = create_export_stores(tmp_path / "ambiguous-run")
    try:
        _add_episode(store, "ep-ambiguous", "data_pipeline")
        _add_truth(
            store,
            episode_id="ep-ambiguous",
            truth_id="truth-ambiguous-a",
            cause="data_pipeline",
            requires_intervention=True,
            features=["amount"],
            segments=["retail"],
            parent_scenario="scenario-two-causes",
        )
        _add_truth(
            store,
            episode_id="ep-ambiguous",
            truth_id="truth-ambiguous-b",
            cause="fraud_surge",
            requires_intervention=True,
            features=["amount"],
            segments=["retail"],
            parent_scenario="scenario-two-causes",
        )
        store.commit()
        incidents = _incident_rows(
            _alert(
                episode_id="ep-ambiguous",
                alert_id="alert-ambiguous",
                feature="amount_avg_7d",
                at="2026-02-05T12:00:00.000000Z",
            ),
            episode_id="ep-ambiguous",
        )

        result = _evaluate(store, incidents, ["ep-ambiguous"])

        assert result.report["incident_matches"][0]["status"] == "ambiguous_match"
        assert result.report["incident_matches"][0]["matched_truth_ids"] == []
        assert result.report["incident_matches"][0]["candidate_truth_ids"] == [
            "truth-ambiguous-a",
            "truth-ambiguous-b",
        ]
        assert result.report["incident_counts"] == {
            "evaluated": 1,
            "matched": 0,
            "ambiguous": 1,
            "unmatched": 0,
            "benign_matched": 0,
        }
        assert result.report["ambiguous_incident_ids"] == [incidents[0]["incident_id"]]
        assert result.report["detection_recall"] == {
            "detected": 2,
            "denominator": 2,
            "recall": 1.0,
            "missed_truth_ids": [],
        }
    finally:
        store.close()


def test_same_feature_multi_cause_truth_components_both_count_for_recall(
    tmp_path: Path,
) -> None:
    store = create_export_stores(tmp_path / "multi-cause-run")
    try:
        _add_episode(store, "ep-multi-cause", "data_pipeline")
        _add_truth(
            store,
            episode_id="ep-multi-cause",
            truth_id="truth-pipeline-component",
            cause="data_pipeline",
            requires_intervention=True,
            features=["amount"],
            segments=["retail"],
            parent_scenario="scenario-two-causes",
        )
        _add_truth(
            store,
            episode_id="ep-multi-cause",
            truth_id="truth-fraud-component",
            cause="fraud_surge",
            requires_intervention=True,
            features=["amount"],
            segments=["retail"],
            parent_scenario="scenario-two-causes",
        )
        store.commit()
        incidents = _incident_rows(
            _alert(
                episode_id="ep-multi-cause",
                alert_id="alert-two-cause-components",
                feature="amount_avg_7d",
                at="2026-02-05T12:00:00.000000Z",
            ),
            episode_id="ep-multi-cause",
        )

        result = _evaluate(store, incidents, ["ep-multi-cause"])

        assert result.report["detection_recall"] == {
            "detected": 2,
            "denominator": 2,
            "recall": 1.0,
            "missed_truth_ids": [],
        }
        assert result.report["incident_matches"][0]["status"] == "ambiguous_match"
        assert result.report["incident_matches"][0]["matched_truth_ids"] == []
        assert result.report["incident_matches"][0]["candidate_truth_ids"] == [
            "truth-fraud-component",
            "truth-pipeline-component",
        ]
    finally:
        store.close()


def test_multiple_incidents_can_match_one_truth_without_becoming_ambiguous(
    tmp_path: Path,
) -> None:
    store = create_export_stores(tmp_path / "multiple-incidents-run")
    try:
        _add_episode(store, "ep-multiple-incidents", "data_pipeline")
        _add_truth(
            store,
            episode_id="ep-multiple-incidents",
            truth_id="truth-shared",
            cause="data_pipeline",
            requires_intervention=True,
            features=["amount"],
            segments=["retail"],
        )
        store.commit()
        incidents = _incident_rows(
            _alert(
                episode_id="ep-multiple-incidents",
                alert_id="alert-shared-early",
                feature="amount_avg_7d",
                at="2026-02-05T12:00:00.000000Z",
            ),
            _alert(
                episode_id="ep-multiple-incidents",
                alert_id="alert-shared-late",
                feature="amount_avg_7d",
                at="2026-02-11T12:00:00.000000Z",
            ),
            episode_id="ep-multiple-incidents",
        )

        result = _evaluate(store, incidents, ["ep-multiple-incidents"])

        assert result.report["incident_counts"]["matched"] == 2
        assert result.report["incident_counts"]["ambiguous"] == 0
        assert all(match["status"] == "matched" for match in result.report["incident_matches"])
        assert result.report["detection_recall"]["detected"] == 1
    finally:
        store.close()


def test_decision_only_snapshot_does_not_extend_detection_recall(tmp_path: Path) -> None:
    store = create_export_stores(tmp_path / "decision-does-not-detect-run")
    try:
        _add_episode(store, "ep-decision-only", "data_pipeline")
        _add_truth(
            store,
            episode_id="ep-decision-only",
            truth_id="truth-after-alert",
            cause="data_pipeline",
            requires_intervention=True,
            features=["amount"],
            segments=["retail"],
        )
        store.commit()
        alert_rows = [
            _alert(
                episode_id="ep-decision-only",
                alert_id="alert-before-effect",
                feature="amount_avg_7d",
                at="2026-02-04T12:00:00.000000Z",
            )
        ]
        without_decision = compute_incidents(alert_rows, episode_id="ep-decision-only")
        incident_id = without_decision.incidents[0]["incident_id"]
        with_decision = compute_incidents(
            alert_rows,
            episode_id="ep-decision-only",
            decision_source=InMemoryDecisionSource(
                [
                    {
                        "decision_id": "decision-human-review",
                        "incident_id": incident_id,
                        "action": "human_review",
                        "decision_at": "2026-02-05T12:00:00.000000Z",
                    }
                ]
            ),
        )
        assert len(with_decision.incidents) == 2
        assert with_decision.incidents[-1]["status"] == "human_review"
        assert with_decision.incidents[-1]["alert_ids_json"] == (
            without_decision.incidents[0]["alert_ids_json"]
        )

        before_result = _evaluate(store, without_decision.incidents, ["ep-decision-only"])
        after_result = _evaluate(store, with_decision.incidents, ["ep-decision-only"])

        assert before_result.report["detection_recall"] == {
            "detected": 0,
            "denominator": 1,
            "recall": 0.0,
            "missed_truth_ids": ["truth-after-alert"],
        }
        assert after_result.report["detection_recall"] == before_result.report[
            "detection_recall"
        ]
        assert after_result.report["incident_matches"][0]["incident_end_at"] == (
            "2026-02-04T12:00:00.000000Z"
        )
    finally:
        store.close()


def test_zero_intervention_denominator_is_na_and_benign_is_reported_separately(
    tmp_path: Path,
) -> None:
    store = create_export_stores(tmp_path / "benign-run")
    try:
        _add_episode(store, "ep-benign", "seasonal")
        _add_truth(
            store,
            episode_id="ep-benign",
            truth_id="truth-seasonal",
            cause="seasonal",
            requires_intervention=False,
            features=["txn_count"],
            segments=["retail"],
        )
        store.commit()
        incidents = _incident_rows(
            _alert(
                episode_id="ep-benign",
                alert_id="alert-seasonal",
                feature="txn_count_24h",
                at="2026-02-05T12:00:00.000000Z",
            ),
            episode_id="ep-benign",
        )

        result = _evaluate(store, incidents, ["ep-benign"])

        assert result.report["detection_recall"] == {
            "detected": 0,
            "denominator": 0,
            "recall": None,
            "missed_truth_ids": [],
        }
        assert result.report["truth_counts"] == {
            "total": 1,
            "requires_intervention": 0,
            "benign": 1,
        }
        assert result.report["incident_counts"]["benign_matched"] == 1
        assert result.report["by_scenario"][0]["recall"] is None
    finally:
        store.close()


def test_unmatched_incident_is_not_relabelled_benign(tmp_path: Path) -> None:
    store = create_export_stores(tmp_path / "unmatched-run")
    try:
        _add_episode(store, "ep-unmatched", "data_pipeline")
        _add_truth(
            store,
            episode_id="ep-unmatched",
            truth_id="truth-intervention",
            cause="data_pipeline",
            requires_intervention=True,
            features=["amount"],
            segments=["retail"],
        )
        store.commit()
        incidents = _incident_rows(
            _alert(
                episode_id="ep-unmatched",
                alert_id="alert-unrelated",
                feature="txn_count_24h",
                at="2026-02-05T12:00:00.000000Z",
            ),
            episode_id="ep-unmatched",
        )

        result = _evaluate(store, incidents, ["ep-unmatched"])

        assert result.report["incident_counts"] == {
            "evaluated": 1,
            "matched": 0,
            "ambiguous": 0,
            "unmatched": 1,
            "benign_matched": 0,
        }
        assert result.report["benign_incident_ids"] == []
        assert result.report["incident_matches"][0]["status"] == "unmatched"
        assert result.report["incident_matches"][0]["candidate_truth_ids"] == []
        assert result.report["detection_recall"]["recall"] == 0.0
    finally:
        store.close()


def test_open_truth_window_ends_at_episode_end_and_incidents_obey_as_of(
    tmp_path: Path,
) -> None:
    store = create_export_stores(tmp_path / "window-run")
    try:
        _add_episode(store, "ep-open-window", "data_pipeline")
        _add_truth(
            store,
            episode_id="ep-open-window",
            truth_id="truth-open-window",
            cause="data_pipeline",
            requires_intervention=True,
            features=["amount"],
            segments=["retail"],
            effect_start_at="2026-02-05T00:00:00.000000Z",
            effect_available_at="2026-02-05T00:00:00.000000Z",
            effect_end_at=None,
        )
        store.commit()
        early = _incident_rows(
            _alert(
                episode_id="ep-open-window",
                alert_id="alert-early",
                feature="amount_avg_7d",
                at="2026-03-01T12:00:00.000000Z",
            ),
            episode_id="ep-open-window",
        )
        future_snapshot = dict(early[-1])
        future_snapshot["event_at"] = "2026-03-03T00:00:00.000000Z"
        future_snapshot["available_at"] = "2026-03-03T00:00:00.000000Z"
        future_snapshot["status"] = "close"

        result = evaluate_detection_recall(
            (*early, future_snapshot),
            private_db_path=store.private_path,
            episode_ids=["ep-open-window"],
            as_of_time="2026-03-02T12:00:00.000000Z",
        )

        assert result.report["detection_recall"]["recall"] == 1.0
        assert result.report["incident_matches"][0]["incident_end_at"] == (
            "2026-03-01T12:00:00.000000Z"
        )
    finally:
        store.close()


def test_evaluation_is_deterministic_and_writes_report_only_under_private_dir(
    tmp_path: Path,
) -> None:
    store = create_export_stores(tmp_path / "deterministic-run")
    try:
        _add_episode(store, "ep-deterministic", "data_pipeline")
        _add_truth(
            store,
            episode_id="ep-deterministic",
            truth_id="truth-deterministic",
            cause="data_pipeline",
            requires_intervention=True,
            features=["amount"],
            segments=["retail"],
        )
        store.commit()
        incidents = _incident_rows(
            _alert(
                episode_id="ep-deterministic",
                alert_id="alert-deterministic",
                feature="amount_avg_7d",
                at="2026-02-05T12:00:00.000000Z",
            ),
            episode_id="ep-deterministic",
        )

        first = _evaluate(store, incidents, ["ep-deterministic"])
        first_contents = first.report_path.read_text(encoding="utf-8")
        second = _evaluate(store, incidents, ["ep-deterministic"])

        assert first.report == second.report
        assert first_contents == second.report_path.read_text(encoding="utf-8")
        assert second.report_path == store.private_path.parent / "reports" / (
            "p3_4_detection_recall.json"
        )
    finally:
        store.close()


def test_p3_3_to_evaluator_smoke_reports_recall_by_cause_and_template(
    tmp_path: Path,
) -> None:
    store = create_export_stores(tmp_path / "p3_4-smoke")
    try:
        _add_episode(store, "ep-smoke-pipeline", "data_pipeline")
        _add_episode(store, "ep-smoke-seasonal", "seasonal")
        _add_truth(
            store,
            episode_id="ep-smoke-pipeline",
            truth_id="truth-smoke-pipeline",
            cause="data_pipeline",
            requires_intervention=True,
            features=["amount"],
            segments=["retail"],
        )
        _add_truth(
            store,
            episode_id="ep-smoke-seasonal",
            truth_id="truth-smoke-seasonal",
            cause="seasonal",
            requires_intervention=False,
            features=["txn_count"],
            segments=["retail"],
        )
        store.commit()

        incidents = (
            *_incident_rows(
                _alert(
                    episode_id="ep-smoke-pipeline",
                    alert_id="alert-smoke-pipeline",
                    feature="amount_avg_7d",
                    at="2026-02-05T12:00:00.000000Z",
                ),
                episode_id="ep-smoke-pipeline",
            ),
            *_incident_rows(
                _alert(
                    episode_id="ep-smoke-seasonal",
                    alert_id="alert-smoke-seasonal",
                    feature="txn_count_24h",
                    at="2026-02-05T12:00:00.000000Z",
                ),
                episode_id="ep-smoke-seasonal",
            ),
        )

        result = _evaluate(
            store,
            incidents,
            ["ep-smoke-pipeline", "ep-smoke-seasonal"],
        )
        by_scenario = {
            (row["cause"], row["template"]): row
            for row in result.report["by_scenario"]
        }

        assert result.report["detection_recall"]["recall"] == 1.0
        assert by_scenario[("data_pipeline", "data_pipeline")]["recall"] == 1.0
        assert by_scenario[("seasonal", "seasonal")]["denominator"] == 0
        assert by_scenario[("seasonal", "seasonal")]["recall"] is None
        assert result.report["incident_counts"]["benign_matched"] == 1
        assert result.report["incident_counts"]["unmatched"] == 0
    finally:
        store.close()


def test_runtime_modules_do_not_import_evaluation() -> None:
    runtime_dirs = ("simulator", "model", "monitoring")
    violations = []
    for directory in runtime_dirs:
        for path in (ROOT / directory).rglob("*.py"):
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
