from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import subprocess
import sys

import pytest
import yaml

from monitoring.metrics import DAILY_METRIC_COLUMNS
from monitoring.signals import MonitoringSignalsError, compute_alerts

ROOT = Path(__file__).resolve().parents[1]


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
        "event_at": as_of_time,
        "available_at": as_of_time,
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
        episode_id="ep-a", profile="smoke",
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
        episode_id="ep-a", profile="smoke",
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
        episode_id="ep-a", profile="smoke",
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
        episode_id="ep-a", profile="smoke",
        as_of_time="2026-02-10T16:59:59.999999Z",
    )

    assert result.alerts == ()
    assert result.report["ignored_count"] == 1
    assert result.report["ignored"][0]["reason"] == "reference_not_available"


def test_same_input_reproduces_alert_rows_and_neutral_ids() -> None:
    rows = [_metric_row()]
    first = compute_alerts(rows, episode_id="ep-a", profile="smoke", as_of_time=rows[0]["as_of_time"])
    second = compute_alerts(rows, episode_id="ep-a", profile="smoke", as_of_time=rows[0]["as_of_time"])

    assert first == second
    assert first.alerts[0]["alert_id"].startswith("alert-")
    assert "pipeline" not in first.alerts[0]["alert_id"]
    assert "truth" not in first.alerts[0]["alert_id"]


def test_metric_that_becomes_available_after_its_snapshot_is_refused_not_alerted() -> None:
    """A3: a daily metric is readable only at its own snapshot; a late row raises, never alerts."""
    on_time = _metric_row(as_of_time="2026-02-10T16:59:59.999999Z", value=0.9)
    result = compute_alerts(
        [on_time], episode_id="ep-a", profile="smoke", as_of_time="2026-02-10T16:59:59.999999Z"
    )
    assert len(result.alerts) == 1
    alert = result.alerts[0]
    assert alert["available_at"] == on_time["available_at"] == alert["as_of_time"]

    late = _metric_row(as_of_time="2026-02-10T16:59:59.999999Z", value=0.9)
    late["available_at"] = "2026-02-11T16:59:59.999999Z"
    for cutoff in ("2026-02-10T16:59:59.999999Z", "2026-02-11T17:00:00.000000Z"):
        with pytest.raises(MonitoringSignalsError, match="available_at"):
            compute_alerts([late], episode_id="ep-a", profile="smoke", as_of_time=cutoff)


def test_mixed_episode_rows_are_rejected() -> None:
    with pytest.raises(MonitoringSignalsError, match="expected 'ep-a'"):
        compute_alerts(
            [_metric_row(episode_id="ep-b")],
            episode_id="ep-a", profile="smoke",
            as_of_time="2026-02-10T16:59:59.999999Z",
        )


def test_volume_alert_uses_one_threshold_for_departure_from_calendar_expectation() -> None:
    reference = json.dumps(
        {
            "baseline_window": "2025-11-01/2026-01-31",
            "business_calendar_expected_relative_change": 0.6,
            "method": "reference_median_1d_count",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    row = _metric_row(
        metric_id="volume.txn_count",
        value=5.0,
        feature="txn_count",
        reference_window=reference,
    )

    result = compute_alerts(
        [row],
        episode_id="ep-a", profile="smoke",
        as_of_time=row["as_of_time"],
    )

    assert len(result.alerts) == 1
    assert result.alerts[0]["metric"] == "volume.txn_count"
    assert result.alerts[0]["feature"] == "txn_count"
    assert result.alerts[0]["value"] == 5.0
    assert result.alerts[0]["threshold"] == 4.25
    assert result.report["volume_rows_evaluated"] == 1


def test_unannounced_daily_volume_uses_its_clean_development_threshold() -> None:
    reference = json.dumps(
        {
            "baseline_window": "2025-11-01/2026-01-31",
            "business_calendar_expected_relative_change": 0.0,
            "method": "reference_median_1d_count",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    rows = [
        _metric_row(
            metric_id="volume.txn_count",
            value=4.25,
            feature="txn_count",
            reference_window=reference,
        ),
        _metric_row(
            metric_id="volume.txn_count",
            as_of_time="2026-02-11T16:59:59.999999Z",
            value=4.250001,
            feature="txn_count",
            reference_window=reference,
        ),
    ]

    result = compute_alerts(
        rows,
        episode_id="ep-a", profile="smoke",
        as_of_time="2026-02-11T16:59:59.999999Z",
    )

    assert len(result.alerts) == 1
    assert result.alerts[0]["value"] == 4.250001
    assert result.alerts[0]["threshold"] == 4.25


def test_volume_alert_suppresses_missing_calendar_expectation() -> None:
    row = _metric_row(
        metric_id="volume.txn_count",
        value=0.8,
        feature="txn_count",
        reference_window="not-a-volume-reference",
    )

    result = compute_alerts(
        [row],
        episode_id="ep-a", profile="smoke",
        as_of_time=row["as_of_time"],
    )

    assert result.alerts == ()
    assert result.report["suppressed"][0]["reasons"] == ["reference_not_available"]


def _declared_thresholds(profile: str) -> tuple[float, float]:
    raw = yaml.safe_load((ROOT / "configs" / "monitoring.yaml").read_text(encoding="utf-8"))
    block = raw["signals"]["profiles"][profile]
    return (
        float(block["psi_threshold"]["value"]),
        float(block["volume_relative_change_threshold"]["value"]),
    )


def test_pilot_profile_applies_its_own_thresholds_with_strict_comparison() -> None:
    # Pinned like the smoke thresholds: the declared pilot values and the alerts they produce.
    assert _declared_thresholds("pilot") == (0.01, 0.21)
    volume_reference = json.dumps(
        {
            "baseline_window": "2025-11-01/2026-01-31",
            "business_calendar_expected_relative_change": 0.0,
            "method": "reference_median_1d_count",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    rows = [
        _metric_row(as_of_time="2026-02-10T16:59:59.999999Z", value=0.01),
        _metric_row(as_of_time="2026-02-11T16:59:59.999999Z", value=0.010001),
        _metric_row(
            metric_id="volume.txn_count",
            feature="txn_count",
            as_of_time="2026-02-10T16:59:59.999999Z",
            value=0.21,
            reference_window=volume_reference,
        ),
        _metric_row(
            metric_id="volume.txn_count",
            feature="txn_count",
            as_of_time="2026-02-11T16:59:59.999999Z",
            value=0.210001,
            reference_window=volume_reference,
        ),
    ]

    result = compute_alerts(
        rows,
        episode_id="ep-a",
        as_of_time="2026-02-11T16:59:59.999999Z",
        profile="pilot",
    )

    assert sorted((alert["metric"], alert["threshold"]) for alert in result.alerts) == [
        ("psi.amount_avg_7d", 0.01),
        ("volume.txn_count", 0.21),
    ]
    assert all(alert["as_of_time"] == "2026-02-11T16:59:59.999999Z" for alert in result.alerts)


def test_missing_partial_or_unknown_profile_thresholds_are_refused_not_borrowed(
    tmp_path: Path,
) -> None:
    raw = yaml.safe_load((ROOT / "configs" / "monitoring.yaml").read_text(encoding="utf-8"))
    no_pilot = copy.deepcopy(raw)
    del no_pilot["signals"]["profiles"]["pilot"]
    partial_pilot = copy.deepcopy(raw)
    del partial_pilot["signals"]["profiles"]["pilot"]["volume_relative_change_threshold"]
    top_level = copy.deepcopy(raw)
    top_level["signals"]["psi_threshold"] = raw["signals"]["profiles"]["smoke"]["psi_threshold"]
    cases = {
        "no_pilot": (no_pilot, "signals.profiles.pilot must declare its thresholds"),
        "partial_pilot": (
            partial_pilot,
            "signals.profiles.pilot.volume_relative_change_threshold must be an object",
        ),
        "top_level": (top_level, "must be declared under signals.profiles"),
    }
    for name, (document, message) in cases.items():
        path = tmp_path / f"{name}.yaml"
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        with pytest.raises(MonitoringSignalsError, match=re.escape(message)):
            compute_alerts(
                [_metric_row()],
                episode_id="ep-a",
                as_of_time="2026-02-10T16:59:59.999999Z",
                profile="pilot",
                monitoring_config_path=path,
            )
    with pytest.raises(MonitoringSignalsError, match="signal profile must be one of"):
        compute_alerts(
            [_metric_row()],
            episode_id="ep-a",
            as_of_time="2026-02-10T16:59:59.999999Z",
            profile="prod",
        )


def test_signals_cli_requires_an_explicit_profile() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "monitoring",
            "signals",
            "--operational-db",
            "unused.sqlite",
            "--episode-id",
            "ep-0001",
            "--as-of-time",
            "2026-02-10T16:59:59.999999Z",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "--profile" in completed.stderr
