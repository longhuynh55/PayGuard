from __future__ import annotations

from dataclasses import replace
from datetime import date
import json
from pathlib import Path

import numpy as np
import pytest

import model.train as train_module
from model.calibrate import fit_calibrator
from model.config import load_pipeline_config
from model.data import ModelExample, ModelSplits, SplitAudit, SplitAuditReport, SplitWindow
from model.features import FeatureMatrix, FeatureSplits
from model.train import artifact_directory_for_run, save_model_run, train_and_calibrate


ROOT = Path(__file__).resolve().parents[1]
FEATURE_NAMES = ("txn_count_1h", "amount_sum_7d")
AS_OF_TIME = "2026-01-31T17:00:00.000000Z"


def _examples(prefix: str, count: int, positive_count: int) -> tuple[ModelExample, ...]:
    negative_count = count - positive_count
    result = []
    for index in range(count):
        target = int(index >= negative_count)
        result.append(
            ModelExample(
                txn_id=f"{prefix}-{index:04d}",
                episode_id="p2-training-test",
                event_at=f"2025-08-{(index % 27) + 1:02d}T12:00:00.000000Z",
                label_state="confirmed_fraud" if target else "confirmed_legit",
                target=target,
            )
        )
    return tuple(result)


def _matrix(examples: tuple[ModelExample, ...]) -> FeatureMatrix:
    count = len(examples)
    return FeatureMatrix(
        examples=examples,
        feature_names=FEATURE_NAMES,
        values=tuple(
            (float(index) / max(count, 1), float(example.target))
            for index, example in enumerate(examples)
        ),
    )


def _model_data(*, calibration_positive: int = 24, calibration_negative: int = 40):
    fit = _examples("fit", 80, 16)
    validation = _examples("validation", 30, 6)
    calibration = _examples(
        "calibration",
        calibration_positive + calibration_negative,
        calibration_positive,
    )
    windows = (
        ("fit", SplitWindow(date(2025, 7, 1), date(2025, 9, 22))),
        ("validation", SplitWindow(date(2025, 9, 22), date(2025, 10, 1))),
        ("calibration", SplitWindow(date(2025, 10, 1), date(2025, 11, 1))),
    )
    audits = tuple(
        SplitAudit(
            name=name,
            window=window,
            candidate_count=len(rows),
            age_eligible_count=len(rows),
            under_horizon_excluded_count=0,
            pending_excluded_count=0,
            sample_count=len(rows),
            positive_count=sum(example.target == 1 for example in rows),
            negative_count=sum(example.target == 0 for example in rows),
        )
        for (name, window), rows in zip(
            windows, (fit, validation, calibration), strict=True
        )
    )
    splits = ModelSplits(
        episode_id="p2-training-test",
        as_of_time=AS_OF_TIME,
        label_policy_version="observed-label-v1",
        fit=fit,
        validation=validation,
        calibration=calibration,
        audit=SplitAuditReport(
            episode_id="p2-training-test",
            as_of_time=AS_OF_TIME,
            horizon_days=90,
            label_policy_version="observed-label-v1",
            splits=audits,
        ),
    )
    features = FeatureSplits(
        episode_id="p2-training-test",
        as_of_time=AS_OF_TIME,
        feature_names=FEATURE_NAMES,
        fit=_matrix(fit),
        validation=_matrix(validation),
        calibration=_matrix(calibration),
    )
    return splits, features


def test_same_seed_produces_reproducible_raw_and_calibrated_scores() -> None:
    splits, features = _model_data()
    config = load_pipeline_config(ROOT / "configs" / "model.yaml")

    first = train_and_calibrate(splits, features, config)
    second = train_and_calibrate(splits, features, config)

    # CPU LightGBM is deterministic here; permit at most 1e-12 numeric drift.
    for split_name in ("fit", "validation", "calibration"):
        assert first.raw_scores[split_name] == pytest.approx(
            second.raw_scores[split_name], rel=0.0, abs=1e-12
        )
        assert first.calibrated_probabilities[split_name] == pytest.approx(
            second.calibrated_probabilities[split_name], rel=0.0, abs=1e-12
        )


def test_calibration_selects_isotonic_and_sigmoid_from_configured_class_support() -> None:
    pipeline_config = load_pipeline_config(ROOT / "configs" / "model.yaml")
    config = pipeline_config.calibration
    assert config.minimum_support == 25
    assert config.support_basis == "each_class"
    assert config.provenance == "assumption, untuned"
    assert pipeline_config.training.provenance == "default, untuned"

    isotonic_count = 60
    isotonic_scores = tuple(index / (isotonic_count - 1) for index in range(isotonic_count))
    isotonic_targets = (0,) * 30 + (1,) * 30
    isotonic = fit_calibrator(isotonic_scores, isotonic_targets, config, seed=42)
    assert isotonic.method == "isotonic"
    assert isotonic.positive_count == isotonic.negative_count == 30
    assert isotonic.predict(isotonic_scores) != isotonic_scores

    fallback_count = 64
    fallback_scores = tuple(index / (fallback_count - 1) for index in range(fallback_count))
    fallback_targets = (0,) * 40 + (1,) * 24
    sigmoid = fit_calibrator(fallback_scores, fallback_targets, config, seed=42)
    assert sigmoid.method == "sigmoid"
    assert sigmoid.positive_count == 24
    assert sigmoid.negative_count == 40
    assert all(0.0 <= probability <= 1.0 for probability in sigmoid.predict(fallback_scores))


def test_early_stopping_uses_only_validation_split(monkeypatch: pytest.MonkeyPatch) -> None:
    splits, features = _model_data()
    config = load_pipeline_config(ROOT / "configs" / "model.yaml")
    captured: dict[str, object] = {}

    class FakePredictor:
        def __init__(self, **parameters):
            captured["parameters"] = parameters
            self.best_iteration_ = 3
            self.n_estimators = parameters["n_estimators"]

        def fit(self, x_fit, y_fit, **kwargs):
            captured["x_fit"] = x_fit.copy()
            captured["y_fit"] = y_fit.copy()
            captured["eval_X"] = kwargs["eval_X"]
            captured["eval_y"] = kwargs["eval_y"]
            captured["callbacks"] = kwargs["callbacks"]
            return self

        def predict_proba(self, values):
            positive = 1.0 / (1.0 + np.exp(-8.0 * (values[:, 0] - 0.5)))
            return np.column_stack((1.0 - positive, positive))

    monkeypatch.setattr(train_module.lgb, "LGBMClassifier", FakePredictor)
    predictor, _, _ = train_module._fit_predictor(features, config.training)

    assert predictor is not None
    assert len(captured["x_fit"]) == len(splits.fit)
    validation_x = captured["eval_X"]
    validation_y = captured["eval_y"]
    assert np.array_equal(validation_x, train_module._to_numpy(features.validation))
    assert np.array_equal(validation_y, np.asarray(features.validation.targets))
    assert len(validation_x) == len(splits.validation)
    assert len(validation_x) != len(splits.calibration)
    assert captured["parameters"]["deterministic"] is True
    assert captured["parameters"]["n_jobs"] == 1
    assert captured["parameters"]["scale_pos_weight"] == pytest.approx(4.0)


def test_calibrator_is_independent_of_fit_and_validation_labels_with_fixed_predictor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    splits, features = _model_data(calibration_positive=30, calibration_negative=30)
    config = load_pipeline_config(ROOT / "configs" / "model.yaml")

    class FixedPredictor:
        best_iteration_ = 1
        n_estimators = 1

        def predict_proba(self, values):
            positive = np.clip(values[:, 0], 0.0, 1.0)
            return np.column_stack((1.0 - positive, positive))

    fixed_predictor = FixedPredictor()
    monkeypatch.setattr(
        train_module,
        "_fit_predictor",
        lambda _features, _training_config: (fixed_predictor, 1, 4.0),
    )
    original = train_and_calibrate(splits, features, config)

    def flipped(matrix: FeatureMatrix) -> FeatureMatrix:
        examples = tuple(
            replace(
                example,
                target=1 - example.target,
                label_state=(
                    "confirmed_legit" if example.target else "confirmed_fraud"
                ),
            )
            for example in matrix.examples
        )
        return replace(matrix, examples=examples)

    changed_fit = flipped(features.fit)
    changed_validation = flipped(features.validation)
    changed_splits = replace(
        splits,
        fit=changed_fit.examples,
        validation=changed_validation.examples,
    )
    changed_features = replace(
        features,
        fit=changed_fit,
        validation=changed_validation,
    )
    changed = train_and_calibrate(changed_splits, changed_features, config)

    assert original.calibrator.method == changed.calibrator.method == "isotonic"
    assert original.raw_scores["calibration"] == changed.raw_scores["calibration"]
    assert original.calibrated_probabilities["calibration"] == (
        changed.calibrated_probabilities["calibration"]
    )
    assert (
        original.metadata["training_data_fingerprint_sha256"]
        != changed.metadata["training_data_fingerprint_sha256"]
    )


def test_raw_and_calibrated_scores_are_separate_and_metadata_is_complete(tmp_path: Path) -> None:
    splits, features = _model_data()
    config = load_pipeline_config(ROOT / "configs" / "model.yaml")
    model_run = train_and_calibrate(splits, features, config)

    assert model_run.raw_scores is not model_run.calibrated_probabilities
    assert model_run.raw_scores["calibration"] != model_run.calibrated_probabilities["calibration"]
    assert model_run.metadata["calibration_method"] == "sigmoid"
    assert model_run.metadata["label_policy_version"] == "observed-label-v1"
    assert model_run.metadata["seed"] == 42
    assert model_run.metadata["split_boundaries"] == {
        "fit": {"start_inclusive": "2025-07-01", "end_exclusive": "2025-09-22"},
        "validation": {"start_inclusive": "2025-09-22", "end_exclusive": "2025-10-01"},
        "calibration": {"start_inclusive": "2025-10-01", "end_exclusive": "2025-11-01"},
    }
    assert len(model_run.metadata["training_data_fingerprint_sha256"]) == 64

    artifact_path = artifact_directory_for_run("test-run", data_root=tmp_path / "data")
    assert artifact_path == tmp_path / "data" / "test-run" / "model"
    artifact_dir = save_model_run(model_run, artifact_path)
    metadata = json.loads((artifact_dir / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["model_version"] == "payguard-lgbm-isotonic-round1"
    assert metadata["calibration_method"] == "sigmoid"
    assert metadata["calibration_support"]["positive_count"] == 24
    assert metadata["conformal"]["fit_split"] == "calibration"
    assert metadata["conformal"]["score_source"] == "raw_predictor_score"
    assert metadata["conformal"]["thresholds"]["legit"]["status"] == (
        "infinite_small_support"
    )
    assert (artifact_dir / "predictor.joblib").is_file()
    assert (artifact_dir / "calibrator.joblib").is_file()
    assert (artifact_dir / "conformal.joblib").is_file()
