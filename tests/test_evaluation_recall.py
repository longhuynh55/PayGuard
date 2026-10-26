from __future__ import annotations

import ast
import json
from pathlib import Path

from evaluation import evaluate_detection_recall
from monitoring.incidents import compute_incidents
from simulator.storage import create_export_stores, insert_private_record


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
            "parent_scenario": None,
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
            "unmatched": 1,
            "benign_matched": 0,
        }
        assert result.report["benign_incident_ids"] == []
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
