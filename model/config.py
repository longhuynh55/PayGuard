"""Validated runtime settings for the P2.3 training and calibration pipeline."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Literal

import yaml


SupportBasis = Literal["total_examples", "each_class"]


@dataclass(frozen=True)
class TrainingConfig:
    model_version: str
    seed: int
    early_stopping_rounds: int
    imbalance_strategy: str
    provenance: str
    lightgbm_parameters: dict[str, int | float | str | bool]


@dataclass(frozen=True)
class CalibrationConfig:
    method: str
    fallback_method: str
    minimum_support: int
    support_basis: SupportBasis
    provenance: str


@dataclass(frozen=True)
class ConformalConfig:
    alpha_legit: float
    alpha_fraud: float
    provenance: str


@dataclass(frozen=True)
class PipelineConfig:
    training: TrainingConfig
    calibration: CalibrationConfig
    conformal: ConformalConfig


def _mapping(value: object, *, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"model config {field} must be a mapping")
    return value


def _non_empty_string(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"model config {field} must be a non-empty string")
    return value


def _positive_integer(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"model config {field} must be a positive integer")
    return value


def _alpha(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"model config {field} must be numeric")
    alpha = float(value)
    if not math.isfinite(alpha) or not 0.0 < alpha < 1.0:
        raise ValueError(f"model config {field} must be finite and in (0, 1)")
    return alpha


def load_pipeline_config(path: str | Path = "configs/model.yaml") -> PipelineConfig:
    """Load model settings without changing split ownership."""
    config_path = Path(path)
    try:
        with config_path.open("r", encoding="utf-8") as stream:
            raw = yaml.safe_load(stream)
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"could not read model config {config_path}: {exc}") from exc

    root = _mapping(raw, field="root")
    training_raw = _mapping(root.get("training"), field="training")
    calibration_raw = _mapping(root.get("calibration"), field="calibration")
    conformal_raw = _mapping(root.get("conformal"), field="conformal")
    lightgbm_raw = _mapping(training_raw.get("lightgbm"), field="training.lightgbm")

    allowed_parameters = {
        "n_estimators",
        "learning_rate",
        "num_leaves",
        "max_depth",
        "min_child_samples",
        "subsample",
        "colsample_bytree",
        "reg_alpha",
        "reg_lambda",
        "deterministic",
        "force_col_wise",
        "n_jobs",
        "verbosity",
    }
    if set(lightgbm_raw) != allowed_parameters:
        missing = sorted(allowed_parameters - set(lightgbm_raw))
        extra = sorted(set(lightgbm_raw) - allowed_parameters)
        raise ValueError(
            "model config training.lightgbm parameter keys differ from the MVP set "
            f"(missing={missing}, extra={extra})"
        )
    boolean_parameters = {"deterministic", "force_col_wise"}
    integer_parameters = {
        "n_estimators",
        "num_leaves",
        "max_depth",
        "min_child_samples",
        "n_jobs",
        "verbosity",
    }
    for name, value in lightgbm_raw.items():
        if name in boolean_parameters:
            if not isinstance(value, bool):
                raise ValueError(f"model config training.lightgbm.{name} must be boolean")
        elif name in integer_parameters:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"model config training.lightgbm.{name} must be an integer")
        elif isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"model config training.lightgbm.{name} must be numeric")

    parameters = dict(lightgbm_raw)
    if parameters["deterministic"] is not True:
        raise ValueError("model config training.lightgbm.deterministic must be true")
    if parameters["force_col_wise"] is not True:
        raise ValueError("model config training.lightgbm.force_col_wise must be true")
    if parameters["n_jobs"] != 1:
        raise ValueError("model config training.lightgbm.n_jobs must be 1 for deterministic fit")
    if parameters["n_estimators"] < 1 or parameters["num_leaves"] < 2:
        raise ValueError("model config training.lightgbm tree counts must be positive")
    if parameters["max_depth"] == 0 or parameters["max_depth"] < -1:
        raise ValueError("model config training.lightgbm.max_depth must be -1 or positive")
    if parameters["min_child_samples"] < 1:
        raise ValueError("model config training.lightgbm.min_child_samples must be positive")
    for name in ("learning_rate", "subsample", "colsample_bytree"):
        if not 0.0 < float(parameters[name]) <= 1.0:
            raise ValueError(f"model config training.lightgbm.{name} must be in (0, 1]")
    for name in ("reg_alpha", "reg_lambda"):
        if float(parameters[name]) < 0.0:
            raise ValueError(f"model config training.lightgbm.{name} must be non-negative")

    seed = training_raw.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("model config training.seed must be a non-negative integer")
    early_stopping_rounds = _positive_integer(
        training_raw.get("early_stopping_rounds"), field="training.early_stopping_rounds"
    )
    imbalance_strategy = training_raw.get("imbalance_strategy")
    if imbalance_strategy != "scale_pos_weight":
        raise ValueError("model config training.imbalance_strategy must be scale_pos_weight")

    method = calibration_raw.get("method")
    fallback_method = calibration_raw.get("fallback_method")
    if method != "isotonic" or fallback_method != "sigmoid":
        raise ValueError("P2.3 calibration must use isotonic with sigmoid fallback")
    support_basis = calibration_raw.get("support_basis")
    if support_basis not in {"total_examples", "each_class"}:
        raise ValueError(
            "model config calibration.support_basis must be total_examples or each_class"
        )

    return PipelineConfig(
        training=TrainingConfig(
            model_version=_non_empty_string(
                training_raw.get("model_version"), field="training.model_version"
            ),
            seed=seed,
            early_stopping_rounds=early_stopping_rounds,
            imbalance_strategy=imbalance_strategy,
            provenance=_non_empty_string(
                training_raw.get("provenance"), field="training.provenance"
            ),
            lightgbm_parameters=parameters,
        ),
        calibration=CalibrationConfig(
            method=method,
            fallback_method=fallback_method,
            minimum_support=_positive_integer(
                calibration_raw.get("minimum_support"), field="calibration.minimum_support"
            ),
            support_basis=support_basis,
            provenance=_non_empty_string(
                calibration_raw.get("provenance"), field="calibration.provenance"
            ),
        ),
        conformal=ConformalConfig(
            alpha_legit=_alpha(
                conformal_raw.get("alpha_legit"), field="conformal.alpha_legit"
            ),
            alpha_fraud=_alpha(
                conformal_raw.get("alpha_fraud"), field="conformal.alpha_fraud"
            ),
            provenance=_non_empty_string(
                conformal_raw.get("provenance"), field="conformal.provenance"
            ),
        ),
    )
