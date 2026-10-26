"""Probability calibration over the locked calibration split only."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression

from model.config import CalibrationConfig


CalibrationMethod = Literal["isotonic", "sigmoid"]


@dataclass(frozen=True)
class FittedCalibrator:
    """A fitted calibration transform and the support used to select it."""

    method: CalibrationMethod
    estimator: IsotonicRegression | LogisticRegression
    sample_count: int
    positive_count: int
    negative_count: int
    support_basis: str
    minimum_support: int

    def predict(self, raw_scores: tuple[float, ...] | list[float]) -> tuple[float, ...]:
        scores = _validate_scores(raw_scores)
        if not len(scores):
            return ()
        if self.method == "isotonic":
            probabilities = self.estimator.predict(scores)
        else:
            probabilities = self.estimator.predict_proba(scores.reshape(-1, 1))[:, 1]
        return tuple(float(value) for value in np.clip(probabilities, 0.0, 1.0))


def _validate_scores(raw_scores: tuple[float, ...] | list[float]) -> np.ndarray:
    try:
        scores = np.asarray(raw_scores, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise ValueError("raw scores must be a one-dimensional numeric sequence") from exc
    if scores.ndim != 1:
        raise ValueError("raw scores must be one-dimensional")
    if not np.isfinite(scores).all() or ((scores < 0.0) | (scores > 1.0)).any():
        raise ValueError("raw scores must be finite values between 0 and 1")
    return scores


def _support_is_sufficient(
    sample_count: int,
    positive_count: int,
    negative_count: int,
    config: CalibrationConfig,
) -> bool:
    if config.support_basis == "total_examples":
        return sample_count >= config.minimum_support
    if config.support_basis == "each_class":
        return min(positive_count, negative_count) >= config.minimum_support
    raise ValueError(f"unsupported calibration support basis: {config.support_basis!r}")


def fit_calibrator(
    calibration_raw_scores: tuple[float, ...] | list[float],
    calibration_targets: tuple[int, ...] | list[int],
    config: CalibrationConfig,
    *,
    seed: int,
) -> FittedCalibrator:
    """Fit isotonic, or its configured sigmoid fallback, from calibration rows.

    This function deliberately accepts only raw calibration scores and their
    targets. Fit and validation labels cannot enter this calibration step.
    """
    scores = _validate_scores(calibration_raw_scores)
    targets = np.asarray(calibration_targets)
    if targets.ndim != 1 or len(targets) != len(scores):
        raise ValueError("calibration targets must align with calibration raw scores")
    if not np.isin(targets, (0, 1)).all():
        raise ValueError("calibration targets must contain only binary labels 0 and 1")
    targets = targets.astype(np.int8, copy=False)
    positive_count = int(np.count_nonzero(targets == 1))
    negative_count = int(np.count_nonzero(targets == 0))
    if positive_count == 0 or negative_count == 0:
        raise ValueError("calibration requires support for both binary classes")

    enough_support = _support_is_sufficient(
        len(scores), positive_count, negative_count, config
    )
    if enough_support:
        estimator: IsotonicRegression | LogisticRegression = IsotonicRegression(
            y_min=0.0,
            y_max=1.0,
            out_of_bounds="clip",
        )
        estimator.fit(scores, targets)
        method: CalibrationMethod = "isotonic"
    else:
        estimator = LogisticRegression(
            solver="lbfgs",
            max_iter=1000,
            random_state=seed,
        )
        estimator.fit(scores.reshape(-1, 1), targets)
        method = "sigmoid"

    return FittedCalibrator(
        method=method,
        estimator=estimator,
        sample_count=len(scores),
        positive_count=positive_count,
        negative_count=negative_count,
        support_basis=config.support_basis,
        minimum_support=config.minimum_support,
    )
