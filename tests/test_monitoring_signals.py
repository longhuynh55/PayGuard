from __future__ import annotations

import pytest

from monitoring.metrics import DAILY_METRIC_COLUMNS
from monitoring.signals import MonitoringSignalsError, compute_alerts


def _metric_row(
    *,
    metric_id: str = "psi.amount_avg_7d",
    episode_id: str = "ep-a",
    as_of_time: str = "2026-02-10T16:59:59.999999Z",
    value: float | None = 0.3,
    n_window: int = 30,
    n_reference: int = 100,
    flags: list[str] | None = None,
    feature: str = "amount_avg_7d",
    segment: str = "all",
    reference_window: str = "2025-11-01/2026-01-31",
) -> dict[str, object]:
    row = {
        "metric_id": metric_id,
        "episode_id": episode_id,
        "as_of_time": as_of_time,
        "business_date": "2026-02-10",
        "feature": feature,
        "segment": segment,
        "value": value,
        "reference_window": reference_window,
        "window_start": "2026-02-04",
        "window_end": "2026-02-10",
        "n_window": n_window,
        "n_reference": n_reference,
        "flags": list(flags or []),
    }
    assert set(row) == set(DAILY_METRIC_COLUMNS)
    return row


def test_psi_threshold_is_strictly_greater_than_point_25() -> None:
    rows = [
        _metric_row(as_of_time="2026-02-10T16:59:59.999999Z", value=0.25),
        _metric_row(as_of_time="2026-02-11T16:59:59.999999Z", value=0.250001),
    ]
    result = compute_alerts(
        rows,
        episode_id="ep-a",
        as_of_time="2026-02-11T16:59:59.999999Z",
    )

    assert len(result.alerts) == 1
    alert = result.alerts[0]
    assert alert["as_of_time"] == "2026-02-11T16:59:59.999999Z"
    assert alert["event_at"] == alert["available_at"] == alert["as_of_time"]
    assert alert["threshold"] == 0.25
    assert result.report["threshold"]["comparison"] == ">"


def test_censored_and_small_sample_metrics_are_suppressed_with_reasons() -> None:
    rows = [
        _metric_row(flags=["window_censored_samples=2"]),
        _metric_row(
            as_of_time="2026-02-11T16:59:59.999999Z",
            n_window=8,
            flags=["small_window_sample"],
        ),
    ]
    result = compute_alerts(
        rows,
        episode_id="ep-a",
        as_of_time="2026-02-11T16:59:59.999999Z",
    )

    assert result.alerts == ()
    assert result.report["suppressed_count"] == 2
    assert result.report["suppressed"][0]["reasons"] == ["window_censored_samples=2"]
    assert "small_window_sample" in result.report["suppressed"][1]["reasons"]


def test_rows_after_requested_snapshot_are_ignored() -> None:
    rows = [
        _metric_row(as_of_time="2026-02-10T16:59:59.999999Z", value=0.3),
        _metric_row(as_of_time="2026-02-11T16:59:59.999999Z", value=0.9),
    ]
    result = compute_alerts(
        rows,
        episode_id="ep-a",
        as_of_time="2026-02-10T16:59:59.999999Z",
    )

    assert len(result.alerts) == 1
    assert result.alerts[0]["as_of_time"] == "2026-02-10T16:59:59.999999Z"
    assert result.report["future_metric_rows_ignored"] == 1
    assert result.report["daily_metric_rows_considered"] == 1


def test_review_rate_without_reference_is_reported_but_never_signaled() -> None:
    row = _metric_row(
        metric_id="model.review_rate",
        value=0.9,
        n_reference=0,
        feature="decision_region",
        segment="model_version=m1",
        reference_window="not_applicable",
    )
    result = compute_alerts(
        [row],
        episode_id="ep-a",
        as_of_time="2026-02-10T16:59:59.999999Z",
    )

    assert result.alerts == ()
    assert result.report["ignored_count"] == 1
    assert result.report["ignored"][0]["reason"] == "reference_not_available"


def test_same_input_reproduces_alert_rows_and_neutral_ids() -> None:
    rows = [_metric_row()]
    first = compute_alerts(rows, episode_id="ep-a", as_of_time=rows[0]["as_of_time"])
    second = compute_alerts(rows, episode_id="ep-a", as_of_time=rows[0]["as_of_time"])

    assert first == second
    assert first.alerts[0]["alert_id"].startswith("alert-")
    assert "pipeline" not in first.alerts[0]["alert_id"]
    assert "truth" not in first.alerts[0]["alert_id"]


def test_mixed_episode_rows_are_rejected() -> None:
    with pytest.raises(MonitoringSignalsError, match="expected 'ep-a'"):
        compute_alerts(
            [_metric_row(episode_id="ep-b")],
            episode_id="ep-a",
            as_of_time="2026-02-10T16:59:59.999999Z",
        )
