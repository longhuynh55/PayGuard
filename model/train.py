"""Deterministic LightGBM training, calibration, and conformal pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any

import joblib
import lightgbm as lgb
import numpy as np

from model.calibrate import FittedCalibrator, fit_calibrator
from model.config import PipelineConfig, TrainingConfig, load_pipeline_config
from model.conformal import (
    FittedMondrianConformal,
    PredictionSet,
    conformal_metadata,
    fit_mondrian_conformal,
    predict_sets,
)
from model.data import ModelSplits
from model.features import FeatureMatrix, FeatureSplits


@dataclass(frozen=True)
class ModelRun:
    """Fitted predictor, calibration and conformal transforms, scores, and metadata."""

    predictor: lgb.LGBMClassifier
    calibrator: FittedCalibrator
    conformal: FittedMondrianConformal
    raw_scores: dict[str, tuple[float, ...]]
    calibrated_probabilities: dict[str, tuple[float, ...]]
    conformal_sets: dict[str, tuple[PredictionSet, ...]]
    metadata: dict[str, Any]


def _validate_inputs(splits: ModelSplits, features: FeatureSplits) -> None:
    if features.episode_id != splits.episode_id:
        raise ValueError("feature matrices and model splits must use the same episode_id")
    if features.as_of_time != splits.as_of_time:
        raise ValueError("feature matrices and model splits must use the same as_of_time")
    expected_matrices = {
        "fit": (splits.fit, features.fit),
        "validation": (splits.validation, features.validation),
        "calibration": (splits.calibration, features.calibration),
    }
    if len(features.feature_names) != len(set(features.feature_names)):
        raise ValueError("feature names must be unique")
    for name, (examples, matrix) in expected_matrices.items():
        if matrix.examples != examples:
            raise ValueError(f"{name} feature rows must align with their model split")
        if matrix.feature_names != features.feature_names:
            raise ValueError(f"{name} feature names differ from the shared feature schema")
        if len(matrix.values) != len(examples):
            raise ValueError(f"{name} feature row count does not match its examples")
        if any(len(row) != len(features.feature_names) for row in matrix.values):
            raise ValueError(f"{name} feature columns do not match the feature schema")


def _to_numpy(matrix: FeatureMatrix) -> np.ndarray:
    if not matrix.values:
        return np.empty((0, len(matrix.feature_names)), dtype=np.float64)
    return np.asarray(
        [
            [np.nan if value is None else float(value) for value in row]
            for row in matrix.values
        ],
        dtype=np.float64,
    )


def _targets(matrix: FeatureMatrix, *, name: str) -> np.ndarray:
    targets = np.asarray(matrix.targets)
    if targets.ndim != 1 or not np.isin(targets, (0, 1)).all():
        raise ValueError(f"{name} targets must contain only binary labels 0 and 1")
    return targets.astype(np.int8, copy=False)


def _training_fingerprint(matrix: FeatureMatrix) -> str:
    records = [
        {
            "txn_id": example.txn_id,
            "episode_id": example.episode_id,
            "event_at": example.event_at,
            "target": example.target,
            "features": [None if value is None else value for value in values],
        }
        for example, values in zip(matrix.examples, matrix.values, strict=True)
    ]
    payload = {
        "feature_names": list(matrix.feature_names),
        "fit_rows": records,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _fit_predictor(
    features: FeatureSplits,
    training_config: TrainingConfig,
) -> tuple[lgb.LGBMClassifier, int, float]:
    x_fit = _to_numpy(features.fit)
    y_fit = _targets(features.fit, name="fit")
    x_validation = _to_numpy(features.validation)
    y_validation = _targets(features.validation, name="validation")
    if len(y_fit) == 0 or len(y_validation) == 0:
        raise ValueError("fit and validation splits must be non-empty")
    positive_count = int(np.count_nonzero(y_fit == 1))
    negative_count = int(np.count_nonzero(y_fit == 0))
    if positive_count == 0 or negative_count == 0:
        raise ValueError("fit split must contain examples from both classes")

    parameters = {
        **training_config.lightgbm_parameters,
        "objective": "binary",
        "random_state": training_config.seed,
        "bagging_seed": training_config.seed,
        "feature_fraction_seed": training_config.seed,
        "data_random_seed": training_config.seed,
        "scale_pos_weight": negative_count / positive_count,
    }
    predictor = lgb.LGBMClassifier(**parameters)
    predictor.fit(
        x_fit,
        y_fit,
        eval_X=x_validation,
        eval_y=y_validation,
        eval_metric="binary_logloss",
        callbacks=[
            lgb.early_stopping(
                stopping_rounds=training_config.early_stopping_rounds,
                first_metric_only=True,
                verbose=False,
            ),
            lgb.log_evaluation(period=0),
        ],
    )
    best_iteration = int(predictor.best_iteration_ or predictor.n_estimators)
    return predictor, best_iteration, negative_count / positive_count


def _raw_scores(predictor: lgb.LGBMClassifier, matrix: FeatureMatrix) -> tuple[float, ...]:
    if not matrix.values:
        return ()
    probabilities = predictor.predict_proba(_to_numpy(matrix))[:, 1]
    if not np.isfinite(probabilities).all():
        raise ValueError("LightGBM produced a non-finite raw score")
    return tuple(float(value) for value in probabilities)


def train_and_calibrate(
    splits: ModelSplits,
    features: FeatureSplits,
    config: PipelineConfig | None = None,
    *,
    config_path: str | Path = "configs/model.yaml",
) -> ModelRun:
    """Fit on fit rows, early stop on validation, calibrate raw scores on October only."""
    pipeline_config = config or load_pipeline_config(config_path)
    _validate_inputs(splits, features)

    predictor, best_iteration, scale_pos_weight = _fit_predictor(
        features,
        pipeline_config.training,
    )
    matrices = {
        "fit": features.fit,
        "validation": features.validation,
        "calibration": features.calibration,
    }
    raw_scores = {
        name: _raw_scores(predictor, matrix)
        for name, matrix in matrices.items()
    }
    # Mondrian thresholds and sets consume only the predictor's raw score.
    # The October calibration split is the sole source of thresholds.
    conformal = fit_mondrian_conformal(
        raw_scores["calibration"],
        features.calibration.targets,
        pipeline_config.conformal,
    )
    conformal_sets = {
        name: predict_sets(conformal, scores) for name, scores in raw_scores.items()
    }
    calibrator = fit_calibrator(
        raw_scores["calibration"],
        features.calibration.targets,
        pipeline_config.calibration,
        seed=pipeline_config.training.seed,
    )
    calibrated_probabilities = {
        name: calibrator.predict(scores)
        for name, scores in raw_scores.items()
    }

    training_data_fingerprint = _training_fingerprint(features.fit)
    split_boundaries = {
        split.name: {
            "start_inclusive": split.window.start_inclusive.isoformat(),
            "end_exclusive": split.window.end_exclusive.isoformat(),
        }
        for split in splits.audit.splits
    }
    metadata: dict[str, Any] = {
        "model_version": pipeline_config.training.model_version,
        "label_policy_version": splits.label_policy_version,
        "seed": pipeline_config.training.seed,
        "episode_id": splits.episode_id,
        "as_of_time": splits.as_of_time,
        "split_boundaries": split_boundaries,
        "training_data_fingerprint_sha256": training_data_fingerprint,
        "feature_names": list(features.feature_names),
        "training_provenance": pipeline_config.training.provenance,
        "training_parameters": {
            **pipeline_config.training.lightgbm_parameters,
            "objective": "binary",
            "scale_pos_weight": scale_pos_weight,
            "early_stopping_rounds": pipeline_config.training.early_stopping_rounds,
            "early_stopping_split": "validation",
            "best_iteration": best_iteration,
        },
        "calibration_method": calibrator.method,
        "calibration_fallback_method": pipeline_config.calibration.fallback_method,
        "calibration_support": {
            "sample_count": calibrator.sample_count,
            "positive_count": calibrator.positive_count,
            "negative_count": calibrator.negative_count,
            "basis": calibrator.support_basis,
            "minimum": calibrator.minimum_support,
            "provenance": pipeline_config.calibration.provenance,
        },
        "conformal": conformal_metadata(conformal),
    }
    return ModelRun(
        predictor=predictor,
        calibrator=calibrator,
        conformal=conformal,
        raw_scores=raw_scores,
        calibrated_probabilities=calibrated_probabilities,
        conformal_sets=conformal_sets,
        metadata=metadata,
    )


def artifact_directory_for_run(run_id: str, *, data_root: str | Path = "data") -> Path:
    """Return the untracked ``data/<run>/model`` destination for a safe run ID."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", run_id) or run_id in {".", ".."}:
        raise ValueError("run_id must contain only letters, digits, dot, underscore, or dash")
    return Path(data_root) / run_id / "model"


def save_model_run(model_run: ModelRun, output_dir: str | Path) -> Path:
    """Write predictor, calibrator, and metadata into a run artifact directory."""
    artifact_dir = Path(output_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    joblib.dump(model_run.predictor, artifact_dir / "predictor.joblib")
    joblib.dump(model_run.calibrator, artifact_dir / "calibrator.joblib")
    joblib.dump(model_run.conformal, artifact_dir / "conformal.joblib")
    with (artifact_dir / "metadata.json").open("w", encoding="utf-8") as stream:
        json.dump(model_run.metadata, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
    return artifact_dir
