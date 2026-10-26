"""Mondrian split conformal prediction for uncalibrated predictor scores."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
import math
from typing import Literal, Sequence

import numpy as np

from model.config import ConformalConfig


ClassLabel = Literal["legit", "fraud"]
PredictionSet = tuple[ClassLabel, ...]
Decision = Literal["legit", "fraud", "review"]


def _minimum_calibration_support(alpha: float) -> int:
    return int(
        (Decimal("1") / Decimal(str(alpha)) - Decimal("1")).to_integral_value(
            rounding=ROUND_CEILING
        )
    )


@dataclass(frozen=True)
class ClassThreshold:
    """One class-conditional finite-sample quantile.

    ``threshold=None`` represents positive infinity: the class is included for
    every valid raw score because its configured alpha is too small for the
    available calibration support.
    """

    label: ClassLabel
    alpha: float
    sample_count: int
    quantile_rank: int
    threshold: float | None

    @property
    def has_finite_threshold(self) -> bool:
        return self.threshold is not None

    @property
    def required_calibration_count(self) -> int:
        return _minimum_calibration_support(self.alpha)

    @property
    def insufficient_support(self) -> bool:
        return self.quantile_rank > self.sample_count


@dataclass(frozen=True)
class FittedMondrianConformal:
    """Class-conditional thresholds learned from raw calibration scores."""

    legit: ClassThreshold
    fraud: ClassThreshold
    provenance: str

    @property
    def thresholds(self) -> tuple[ClassThreshold, ClassThreshold]:
        return self.legit, self.fraud

    def threshold_for(self, label: ClassLabel) -> ClassThreshold:
        if label == "legit":
            return self.legit
        if label == "fraud":
            return self.fraud
        raise ValueError(f"unsupported conformal class: {label!r}")


@dataclass(frozen=True)
class ClassCoverage:
    covered_count: int
    sample_count: int
    rate: float | None


@dataclass(frozen=True)
class ConformalReport:
    """Coverage and decision rates on a labeled evaluation cohort."""

    thresholds: tuple[ClassThreshold, ClassThreshold]
    provenance: str
    coverage_by_class: dict[ClassLabel, ClassCoverage]
    false_approval_count: int
    fraud_count: int
    false_decline_count: int
    legit_count: int
    review_count: int
    sample_count: int

    @property
    def insufficient_support_by_class(self) -> dict[ClassLabel, bool]:
        return {
            threshold.label: threshold.insufficient_support
            for threshold in self.thresholds
        }

    @property
    def false_approval_rate(self) -> float | None:
        return (
            self.false_approval_count / self.fraud_count
            if self.fraud_count
            else None
        )

    @property
    def false_decline_rate(self) -> float | None:
        return (
            self.false_decline_count / self.legit_count
            if self.legit_count
            else None
        )

    @property
    def review_rate(self) -> float | None:
        return self.review_count / self.sample_count if self.sample_count else None


def _validate_raw_scores(raw_scores: Sequence[float]) -> np.ndarray:
    try:
        scores = np.asarray(raw_scores, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise ValueError("raw scores must be a one-dimensional numeric sequence") from exc
    if scores.ndim != 1:
        raise ValueError("raw scores must be one-dimensional")
    if not np.isfinite(scores).all() or ((scores < 0.0) | (scores > 1.0)).any():
        raise ValueError("raw scores must be finite values between 0 and 1")
    return scores


def _validate_targets(targets: Sequence[int], *, expected_count: int) -> np.ndarray:
    target_array = np.asarray(targets)
    if target_array.ndim != 1 or len(target_array) != expected_count:
        raise ValueError("targets must be a one-dimensional sequence aligned with scores")
    if not np.isin(target_array, (0, 1)).all():
        raise ValueError("targets must contain only binary labels 0 (legit) and 1 (fraud)")
    return target_array.astype(np.int8, copy=False)


def _finite_sample_rank(sample_count: int, alpha: float) -> int:
    # Decimal arithmetic avoids floating point rounding turning an exact
    # integer rank into the next order statistic.
    tail_probability = Decimal("1") - Decimal(str(alpha))
    return int(
        (Decimal(sample_count + 1) * tail_probability).to_integral_value(
            rounding=ROUND_CEILING
        )
    )


def _fit_class_threshold(
    label: ClassLabel,
    nonconformity_scores: np.ndarray,
    alpha: float,
) -> ClassThreshold:
    if (
        isinstance(alpha, bool)
        or not isinstance(alpha, (int, float))
        or not math.isfinite(alpha)
        or not 0.0 < alpha < 1.0
    ):
        raise ValueError("class alpha must be finite and in (0, 1)")
    ordered = np.sort(nonconformity_scores)
    sample_count = int(len(ordered))
    quantile_rank = _finite_sample_rank(sample_count, alpha)
    threshold = (
        float(ordered[quantile_rank - 1]) if quantile_rank <= sample_count else None
    )
    return ClassThreshold(
        label=label,
        alpha=alpha,
        sample_count=sample_count,
        quantile_rank=quantile_rank,
        threshold=threshold,
    )


def fit_mondrian_conformal(
    calibration_raw_scores: Sequence[float],
    calibration_targets: Sequence[int],
    config: ConformalConfig,
) -> FittedMondrianConformal:
    """Fit class-conditional thresholds from raw scores and calibration labels.

    Target ``0`` is legit and target ``1`` is fraud. The nonconformity score
    for legit is the predictor's raw fraud score; for fraud it is one minus
    that raw score. No calibrated probabilities or calibrator are accepted.
    """
    scores = _validate_raw_scores(calibration_raw_scores)
    targets = _validate_targets(calibration_targets, expected_count=len(scores))

    legit_scores = scores[targets == 0]
    fraud_scores = 1.0 - scores[targets == 1]
    legit_threshold = _fit_class_threshold(
        "legit", legit_scores, config.alpha_legit
    )
    fraud_threshold = _fit_class_threshold(
        "fraud", fraud_scores, config.alpha_fraud
    )
    return FittedMondrianConformal(
        legit=legit_threshold,
        fraud=fraud_threshold,
        provenance=config.provenance,
    )


def prediction_set_for_score(
    conformal: FittedMondrianConformal,
    raw_score: float,
) -> PredictionSet:
    """Return the class set for one uncalibrated raw fraud score."""
    score_array = _validate_raw_scores((raw_score,))
    score = float(score_array[0])
    labels: list[ClassLabel] = []
    if conformal.legit.threshold is None or score <= conformal.legit.threshold:
        labels.append("legit")
    fraud_nonconformity = 1.0 - score
    if conformal.fraud.threshold is None or fraud_nonconformity <= conformal.fraud.threshold:
        labels.append("fraud")
    return tuple(labels)


def predict_sets(
    conformal: FittedMondrianConformal,
    raw_scores: Sequence[float],
) -> tuple[PredictionSet, ...]:
    """Predict class sets using raw predictor scores only."""
    scores = _validate_raw_scores(raw_scores)
    prediction_sets: list[PredictionSet] = []
    for raw_score in scores:
        score = float(raw_score)
        labels: list[ClassLabel] = []
        if conformal.legit.threshold is None or score <= conformal.legit.threshold:
            labels.append("legit")
        if conformal.fraud.threshold is None or 1.0 - score <= conformal.fraud.threshold:
            labels.append("fraud")
        prediction_sets.append(tuple(labels))
    return tuple(prediction_sets)


def decision_for_set(prediction_set: Sequence[str]) -> Decision:
    """Map singleton sets to their class; empty and multi-class sets need review."""
    if tuple(prediction_set) == ("legit",):
        return "legit"
    if tuple(prediction_set) == ("fraud",):
        return "fraud"
    return "review"


def evaluate_conformal(
    conformal: FittedMondrianConformal,
    raw_scores: Sequence[float],
    targets: Sequence[int],
) -> ConformalReport:
    """Report class coverage and action rates on a separate labeled cohort.

    False approval is fraud decided as legit, divided by all fraud examples.
    False decline is legit decided as fraud, divided by all legit examples.
    Review rate counts empty and multi-class sets over all examples.
    """
    scores = _validate_raw_scores(raw_scores)
    target_array = _validate_targets(targets, expected_count=len(scores))
    prediction_sets = predict_sets(conformal, scores)
    decisions = tuple(decision_for_set(prediction_set) for prediction_set in prediction_sets)

    coverage_by_class: dict[ClassLabel, ClassCoverage] = {}
    for label, target in (("legit", 0), ("fraud", 1)):
        indexes = np.flatnonzero(target_array == target)
        covered_count = sum(label in prediction_sets[int(index)] for index in indexes)
        sample_count = int(len(indexes))
        coverage_by_class[label] = ClassCoverage(
            covered_count=covered_count,
            sample_count=sample_count,
            rate=covered_count / sample_count if sample_count else None,
        )

    fraud_indexes = np.flatnonzero(target_array == 1)
    legit_indexes = np.flatnonzero(target_array == 0)
    false_approval_count = sum(decisions[int(index)] == "legit" for index in fraud_indexes)
    false_decline_count = sum(decisions[int(index)] == "fraud" for index in legit_indexes)
    review_count = sum(decision == "review" for decision in decisions)
    return ConformalReport(
        thresholds=(conformal.legit, conformal.fraud),
        provenance=conformal.provenance,
        coverage_by_class=coverage_by_class,
        false_approval_count=false_approval_count,
        fraud_count=int(len(fraud_indexes)),
        false_decline_count=false_decline_count,
        legit_count=int(len(legit_indexes)),
        review_count=review_count,
        sample_count=len(scores),
    )


def format_conformal_report(report: ConformalReport) -> str:
    """Format a report with denominators and explicit infinite-threshold reasons."""
    lines = ["Mondrian conformal report", f"alpha provenance: {report.provenance}"]
    for threshold in report.thresholds:
        if threshold.threshold is None:
            threshold_text = (
                "+infinity (quantile rank "
                f"{threshold.quantile_rank} exceeds calibration n={threshold.sample_count}; "
                "class is included for every raw score; configured alpha unchanged)"
            )
        else:
            threshold_text = f"{threshold.threshold:.6f}"
        lines.append(
            f"{threshold.label}: alpha={threshold.alpha:g}, calibration n="
            f"{threshold.sample_count}, minimum n={threshold.required_calibration_count}, "
            f"rank={threshold.quantile_rank}, "
            f"insufficient_support={str(threshold.insufficient_support).lower()}, "
            f"threshold={threshold_text}"
        )

    for label in ("legit", "fraud"):
        coverage = report.coverage_by_class[label]
        rate_text = "n/a" if coverage.rate is None else f"{coverage.rate:.6f}"
        lines.append(
            f"coverage {label}: {rate_text} "
            f"({coverage.covered_count}/{coverage.sample_count})"
        )
    false_approval = (
        "n/a" if report.false_approval_rate is None else f"{report.false_approval_rate:.6f}"
    )
    false_decline = (
        "n/a" if report.false_decline_rate is None else f"{report.false_decline_rate:.6f}"
    )
    review_rate = "n/a" if report.review_rate is None else f"{report.review_rate:.6f}"
    lines.extend(
        (
            f"false approval: {false_approval} "
            f"({report.false_approval_count}/{report.fraud_count} fraud)",
            f"false decline: {false_decline} "
            f"({report.false_decline_count}/{report.legit_count} legit)",
            f"review: {review_rate} ({report.review_count}/{report.sample_count})",
        )
    )
    return "\n".join(lines)


def conformal_metadata(conformal: FittedMondrianConformal) -> dict[str, object]:
    """Return JSON-safe threshold metadata for a model artifact."""
    thresholds: dict[str, dict[str, object]] = {}
    for threshold in conformal.thresholds:
        thresholds[threshold.label] = {
            "alpha": threshold.alpha,
            "calibration_count": threshold.sample_count,
            "quantile_rank": threshold.quantile_rank,
            "threshold": threshold.threshold,
            "insufficient_support": threshold.insufficient_support,
            "status": "finite" if threshold.threshold is not None else "infinite_small_support",
        }
    return {
        "method": "mondrian_split_conformal",
        "score_source": "raw_predictor_score",
        "fit_split": "calibration",
        "provenance": conformal.provenance,
        "thresholds": thresholds,
    }

