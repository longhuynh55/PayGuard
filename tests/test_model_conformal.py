from __future__ import annotations

import math
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from model.config import ConformalConfig, load_pipeline_config
from model.conformal import (
    ClassThreshold,
    FittedMondrianConformal,
    decision_for_set,
    evaluate_conformal,
    fit_mondrian_conformal,
    format_conformal_report,
    predict_sets,
)
from model.data import ModelExample, ModelSplits, SplitAudit, SplitAuditReport, SplitWindow
from model.features import FeatureMatrix, FeatureSplits
import model.train as train_module
from model.train import train_and_calibrate


ROOT = Path(__file__).resolve().parents[1]
PROVENANCE = "bản 24/10, giả định cần đo lại trên split mới, chưa tune"


def test_alpha_values_and_provenance_load_from_model_config() -> None:
    config = load_pipeline_config(ROOT / "configs" / "model.yaml").conformal

    assert config.alpha_legit == 0.005
    assert config.alpha_fraud == 0.20
    assert config.provenance == PROVENANCE


def test_finite_sample_rank_and_small_support_keep_alpha_unchanged() -> None:
    config = ConformalConfig(
        alpha_legit=0.005,
        alpha_fraud=0.20,
        provenance=PROVENANCE,
    )
    calibration_raw_scores = tuple(index / 200 for index in range(128)) + tuple(
        0.60 + index / 1000 for index in range(24)
    )
    calibration_targets = (0,) * 128 + (1,) * 24

    fitted = fit_mondrian_conformal(
        calibration_raw_scores,
        calibration_targets,
        config,
    )

    assert fitted.legit.quantile_rank == math.ceil((128 + 1) * (1 - 0.005)) == 129
    assert fitted.legit.sample_count == 128
    assert fitted.legit.threshold is None
    assert fitted.fraud.quantile_rank == math.ceil((24 + 1) * (1 - 0.20)) == 20
    assert fitted.fraud.sample_count == 24
    expected_fraud_nonconformity = sorted(1 - score for score in calibration_raw_scores[128:])
    assert fitted.fraud.threshold == pytest.approx(expected_fraud_nonconformity[19])

    report = evaluate_conformal(fitted, (0.25,), (0,))
    formatted = format_conformal_report(report)
    assert f"alpha provenance: {PROVENANCE}" in formatted
    assert "quantile rank 129 exceeds calibration n=128" in formatted
    assert "class is included for every raw score" in formatted
    assert "configured alpha unchanged" in formatted


def test_empty_and_multi_class_sets_map_to_review() -> None:
    empty_set_model = FittedMondrianConformal(
        legit=ClassThreshold("legit", 0.2, 10, 8, 0.1),
        fraud=ClassThreshold("fraud", 0.2, 10, 8, 0.1),
        provenance=PROVENANCE,
    )
    broad_set_model = FittedMondrianConformal(
        legit=ClassThreshold("legit", 0.2, 10, 8, 0.75),
        fraud=ClassThreshold("fraud", 0.2, 10, 8, 0.75),
        provenance=PROVENANCE,
    )

    empty_set = predict_sets(empty_set_model, (0.5,))[0]
    multi_class_set = predict_sets(broad_set_model, (0.5,))[0]

    assert empty_set == ()
    assert decision_for_set(empty_set) == "review"
    assert multi_class_set == ("legit", "fraud")
    assert decision_for_set(multi_class_set) == "review"
    assert decision_for_set(("legit",)) == "legit"
    assert decision_for_set(("fraud",)) == "fraud"


def test_report_uses_class_denominators_and_counts_review_sets() -> None:
    fitted = FittedMondrianConformal(
        legit=ClassThreshold("legit", 0.2, 10, 8, 0.25),
        fraud=ClassThreshold("fraud", 0.2, 10, 8, 0.25),
        provenance=PROVENANCE,
    )

    report = evaluate_conformal(
        fitted,
        raw_scores=(0.1, 0.9, 0.5, 0.5),
        targets=(1, 0, 0, 1),
    )

    assert report.coverage_by_class["legit"].covered_count == 0
    assert report.coverage_by_class["legit"].sample_count == 2
    assert report.coverage_by_class["fraud"].covered_count == 0
    assert report.coverage_by_class["fraud"].sample_count == 2
    assert report.false_approval_count == 1
    assert report.fraud_count == 2
    assert report.false_approval_rate == 0.5
    assert report.false_decline_count == 1
    assert report.legit_count == 2
    assert report.false_decline_rate == 0.5
    assert report.review_count == 2
    assert report.review_rate == 0.5
    formatted = format_conformal_report(report)
    assert "false approval: 0.500000 (1/2 fraud)" in formatted
    assert "false decline: 0.500000 (1/2 legit)" in formatted
    assert "review: 0.500000 (2/4)" in formatted


def _draw_exchangeable_rows(
    rng: np.random.Generator,
    sample_count: int,
) -> tuple[tuple[float, ...], tuple[int, ...]]:
    targets = rng.binomial(1, 0.2, size=sample_count).astype(np.int8)
    scores = np.empty(sample_count, dtype=np.float64)
    legit_mask = targets == 0
    fraud_mask = targets == 1
    scores[legit_mask] = rng.beta(2.0, 8.0, size=int(legit_mask.sum()))
    scores[fraud_mask] = rng.beta(8.0, 2.0, size=int(fraud_mask.sum()))
    return tuple(float(value) for value in scores), tuple(int(value) for value in targets)


def test_class_coverage_on_seeded_iid_holdout_is_within_sampling_error() -> None:
    rng = np.random.default_rng(20261026)
    calibration_scores, calibration_targets = _draw_exchangeable_rows(rng, 50_000)
    holdout_scores, holdout_targets = _draw_exchangeable_rows(rng, 50_000)
    config = ConformalConfig(
        alpha_legit=0.005,
        alpha_fraud=0.20,
        provenance=PROVENANCE,
    )

    fitted = fit_mondrian_conformal(
        calibration_scores,
        calibration_targets,
        config,
    )
    report = evaluate_conformal(fitted, holdout_scores, holdout_targets)

    # Calibration and holdout are independent draws from the same fixed i.i.d.
    # generator. The tolerance includes four standard errors from both samples
    # and the finite-sample rank step; it is not adjusted from the observed result.
    for label, target_value, alpha in (("legit", 0, 0.005), ("fraud", 1, 0.20)):
        calibration_support = calibration_targets.count(target_value)
        holdout_support = holdout_targets.count(target_value)
        expected_coverage = 1.0 - alpha
        tolerance = (
            4.0
            * math.sqrt(
                expected_coverage
                * alpha
                * (1.0 / calibration_support + 1.0 / holdout_support)
            )
            + 1.0 / calibration_support
            + 1.0 / holdout_support
        )
        assert fitted.threshold_for(label).sample_count == calibration_support
        assert report.coverage_by_class[label].sample_count == holdout_support
        assert report.coverage_by_class[label].rate == pytest.approx(
            expected_coverage,
            abs=tolerance,
        )

    repeated = fit_mondrian_conformal(
        calibration_scores,
        calibration_targets,
        config,
    )
    assert repeated == fitted


def test_fit_thresholds_depend_only_on_calibration_inputs() -> None:
    config = ConformalConfig(
        alpha_legit=0.1,
        alpha_fraud=0.2,
        provenance=PROVENANCE,
    )
    calibration_scores = (0.1, 0.2, 0.3, 0.7, 0.8, 0.9)
    calibration_targets = (0, 0, 0, 1, 1, 1)
    holdout_scores = (0.05, 0.95)
    holdout_targets = (1, 0)

    fitted = fit_mondrian_conformal(calibration_scores, calibration_targets, config)
    first_holdout_report = evaluate_conformal(fitted, holdout_scores, holdout_targets)
    changed_holdout_report = evaluate_conformal(
        fitted,
        tuple(1.0 - score for score in holdout_scores),
        tuple(1 - target for target in holdout_targets),
    )
    fitted_again = fit_mondrian_conformal(calibration_scores, calibration_targets, config)

    assert fitted_again == fitted
    assert first_holdout_report.thresholds == changed_holdout_report.thresholds


def _training_fixture() -> tuple[ModelSplits, FeatureSplits]:
    split_specs = (
        ("fit", 40, 10, date(2025, 7, 1), date(2025, 9, 22)),
        ("validation", 20, 5, date(2025, 9, 22), date(2025, 10, 1)),
        ("calibration", 30, 10, date(2025, 10, 1), date(2025, 11, 1)),
    )
    examples_by_split: dict[str, tuple[ModelExample, ...]] = {}
    matrices_by_split: dict[str, FeatureMatrix] = {}
    audits = []
    for name, count, fraud_count, start, end in split_specs:
        examples = tuple(
            ModelExample(
                txn_id=f"{name}-{index}",
                episode_id="conformal-integration",
                event_at=f"{start.isoformat()}T12:00:00.000000Z",
                label_state="confirmed_fraud" if index < fraud_count else "confirmed_legit",
                target=int(index < fraud_count),
            )
            for index in range(count)
        )
        examples_by_split[name] = examples
        matrices_by_split[name] = FeatureMatrix(
            examples=examples,
            feature_names=("score_feature",),
            values=tuple((index / count,) for index in range(count)),
        )
        audits.append(
            SplitAudit(
                name=name,
                window=SplitWindow(start, end),
                candidate_count=count,
                age_eligible_count=count,
                under_horizon_excluded_count=0,
                pending_excluded_count=0,
                sample_count=count,
                positive_count=fraud_count,
                negative_count=count - fraud_count,
            )
        )
    splits = ModelSplits(
        episode_id="conformal-integration",
        as_of_time="2026-01-31T17:00:00.000000Z",
        label_policy_version="observed-label-v1",
        fit=examples_by_split["fit"],
        validation=examples_by_split["validation"],
        calibration=examples_by_split["calibration"],
        audit=SplitAuditReport(
            episode_id="conformal-integration",
            as_of_time="2026-01-31T17:00:00.000000Z",
            horizon_days=90,
            label_policy_version="observed-label-v1",
            splits=tuple(audits),
        ),
    )
    features = FeatureSplits(
        episode_id=splits.episode_id,
        as_of_time=splits.as_of_time,
        feature_names=("score_feature",),
        fit=matrices_by_split["fit"],
        validation=matrices_by_split["validation"],
        calibration=matrices_by_split["calibration"],
    )
    return splits, features


def test_training_conformal_uses_raw_calibration_scores_and_ignores_calibrator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    splits, features = _training_fixture()
    pipeline_config = load_pipeline_config(ROOT / "configs" / "model.yaml")
    captured: list[tuple[tuple[float, ...], tuple[int, ...]]] = []
    original_fit = train_module.fit_mondrian_conformal

    def capture_fit(raw_scores, targets, config):
        captured.append((tuple(raw_scores), tuple(targets)))
        return original_fit(raw_scores, targets, config)

    class FixedPredictor:
        best_iteration_ = 1
        n_estimators = 1

        def predict_proba(self, values):
            positive = np.clip(values[:, 0], 0.0, 1.0)
            return np.column_stack((1.0 - positive, positive))

    class ArbitraryCalibrator:
        method = "isotonic"
        sample_count = 30
        positive_count = 10
        negative_count = 20
        support_basis = "each_class"
        minimum_support = 25

        def __init__(self, offset: float) -> None:
            self.offset = offset

        def predict(self, scores):
            return tuple(float(np.clip(score + self.offset, 0.0, 1.0)) for score in scores)

    fixed_predictor = FixedPredictor()
    monkeypatch.setattr(
        train_module,
        "_fit_predictor",
        lambda _features, _training_config: (fixed_predictor, 1, 3.0),
    )
    monkeypatch.setattr(train_module, "fit_mondrian_conformal", capture_fit)
    monkeypatch.setattr(
        train_module,
        "fit_calibrator",
        lambda *_args, **_kwargs: ArbitraryCalibrator(0.0),
    )
    first = train_and_calibrate(splits, features, pipeline_config)

    monkeypatch.setattr(
        train_module,
        "fit_calibrator",
        lambda *_args, **_kwargs: ArbitraryCalibrator(0.4),
    )
    second = train_and_calibrate(splits, features, pipeline_config)

    assert len(captured) == 2
    for scores, targets in captured:
        assert scores == first.raw_scores["calibration"]
        assert targets == features.calibration.targets
        assert len(scores) == len(features.calibration.examples)
        assert len(scores) != len(features.fit.examples)
    assert first.calibrated_probabilities["calibration"] != second.calibrated_probabilities[
        "calibration"
    ]
    assert first.conformal == second.conformal
    assert first.conformal_sets == second.conformal_sets
    assert first.conformal_sets["calibration"] == predict_sets(
        first.conformal,
        first.raw_scores["calibration"],
    )
    assert first.metadata["conformal"]["fit_split"] == "calibration"
    assert first.metadata["conformal"]["score_source"] == "raw_predictor_score"
