from __future__ import annotations

import csv
from datetime import UTC, date, datetime, time, timedelta
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from zoneinfo import ZoneInfo

import pytest

from evaluation import evaluate_detection_recall
from model.evaluate import score_monitoring_episodes, train_operational_model
from monitoring.incidents import compute_incidents
from monitoring.metrics import compute_daily_metrics, population_stability_index
from monitoring.signals import compute_alerts
from monitoring.store import persist_monitoring_rows
from simulator.asof import read_as_of
from simulator.config import load_episode_config, load_simulator_config
from simulator.episodes import generate_episodes
from simulator.storage import StorePair, create_export_stores, insert_operational_record


ROOT = Path(__file__).resolve().parents[1]
BUSINESS_TZ = ZoneInfo("Asia/Ho_Chi_Minh")


def _local_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _add_transaction(
    operational,
    *,
    txn_id: str,
    episode_id: str,
    created_at: datetime,
    available_at: datetime | None = None,
    amount: int | None = 100_000,
    customer_id: str = "customer-base",
) -> None:
    created = _local_timestamp(created_at)
    available = _local_timestamp(available_at or created_at)
    insert_operational_record(
        operational,
        "transactions",
        {
            "txn_id": txn_id,
            "episode_id": episode_id,
            "customer_id": customer_id,
            "merchant_id": "merchant-base",
            "created_at": created,
            "event_at": created,
            "available_at": available,
            "amount": amount,
            "channel": "qr",
        },
    )


def _add_customer(operational, *, customer_id: str, episode_id: str, observed_at: datetime) -> None:
    timestamp = _local_timestamp(observed_at)
    insert_operational_record(
        operational,
        "customers",
        {
            "customer_id": customer_id,
            "episode_id": episode_id,
            "event_at": timestamp,
            "available_at": timestamp,
            "segment": "retail",
            "province": "HCM",
            "age_band": "35-44",
        },
    )


def _add_review_score(
    operational,
    *,
    txn_id: str,
    episode_id: str,
    decision_at: datetime,
    available_at: datetime | None = None,
    model_version: str = "test-model",
) -> None:
    timestamp = _local_timestamp(decision_at)
    insert_operational_record(
        operational,
        "model_scores",
        {
            "txn_id": txn_id,
            "episode_id": episode_id,
            "decision_at": timestamp,
            "event_at": timestamp,
            "available_at": _local_timestamp(available_at or decision_at),
            "raw_score": 0.5,
            "calibrated_prob": 0.5,
            "conformal_set": [],
            "decision_region": "review",
            "model_version": model_version,
            "label_policy_version": "test-policy",
        },
    )


def _make_operational_store(path: Path, *, episode_ids: tuple[str, ...] = ("ep-a", "ep-b")) -> StorePair:
    store = create_export_stores(path)
    for index, episode_id in enumerate(episode_ids):
        insert_operational_record(
            store.operational,
            "episodes",
            {"episode_id": episode_id, "role": ("development", "calibration", "test")[index % 3]},
        )
    insert_operational_record(
        store.operational,
        "customers",
        {
            "customer_id": "customer-base",
            "episode_id": "base",
            "event_at": "2025-07-01T00:00:00Z",
            "available_at": "2025-07-01T00:00:00Z",
            "segment": "retail",
            "province": "HCM",
            "age_band": "35-44",
        },
    )
    insert_operational_record(
        store.operational,
        "merchants",
        {
            "merchant_id": "merchant-base",
            "episode_id": "base",
            "event_at": "2025-07-01T00:00:00Z",
            "available_at": "2025-07-01T00:00:00Z",
            "mcc": "5411",
            "province": "HCM",
        },
    )
    reference_day = date(2025, 11, 1)
    counter = 0
    while reference_day < date(2026, 2, 1):
        local_time = datetime.combine(reference_day, time(12, 0), tzinfo=BUSINESS_TZ)
        _add_transaction(
            store.operational,
            txn_id=f"txn-base-{counter:03d}",
            episode_id="base",
            created_at=local_time,
            amount=100_000 + counter * 1_000,
        )
        counter += 1
        reference_day += timedelta(days=3)
    return store


@pytest.fixture
def operational_store(tmp_path: Path):
    store = _make_operational_store(tmp_path / "monitoring-fixture")
    try:
        yield store
    finally:
        store.close()


def _daily_row(rows, *, metric_id: str, episode_id: str, business_date: str):
    selected = [
        row
        for row in rows
        if row["metric_id"] == metric_id
        and row["episode_id"] == episode_id
        and row["business_date"] == business_date
    ]
    assert len(selected) == 1
    return selected[0]


def test_psi_is_zero_for_same_distribution_and_positive_for_clear_shift() -> None:
    reference = tuple(float(value) for value in range(1, 101))
    assert population_stability_index(
        reference, reference, bins=10, epsilon=1e-6
    ) == pytest.approx(0.0)
    assert population_stability_index(
        tuple(value + 1_000 for value in reference),
        reference,
        bins=10,
        epsilon=1e-6,
    ) > 0.5
    assert population_stability_index(
        [100.0] * 20,
        [1.0] * 20,
        bins=10,
        epsilon=1e-6,
    ) > 0.5


def test_daily_metrics_are_deterministic_and_use_vietnam_business_date(
    operational_store: StorePair,
) -> None:
    created_at = datetime(2026, 2, 10, 0, 30, tzinfo=BUSINESS_TZ)
    assert created_at.astimezone(UTC).date() == date(2026, 2, 9)
    _add_transaction(
        operational_store.operational,
        txn_id="txn-vn-midnight-boundary",
        episode_id="ep-a",
        created_at=created_at,
    )
    _add_transaction(
        operational_store.operational,
        txn_id="txn-vn-late-evening",
        episode_id="ep-a",
        created_at=datetime(2026, 2, 10, 23, 30, tzinfo=BUSINESS_TZ),
    )

    first = compute_daily_metrics(operational_store.operational, episode_ids=("ep-a",))
    second = compute_daily_metrics(operational_store.operational, episode_ids=("ep-a",))
    assert first == second
    metric = _daily_row(
        first,
        metric_id="psi.txn_count_24h",
        episode_id="ep-a",
        business_date="2026-02-10",
    )
    assert metric["n_window"] == 2
    assert metric["window_start"] == "2026-02-04"
    assert metric["window_end"] == "2026-02-10"
    assert metric["as_of_time"] == "2026-02-10T16:59:59.999999Z"
    assert "small_window_sample" in metric["flags"]


def test_daily_metric_does_not_change_for_rows_available_after_as_of(
    operational_store: StorePair,
) -> None:
    visible_time = datetime(2026, 2, 9, 12, 0, tzinfo=BUSINESS_TZ)
    _add_transaction(
        operational_store.operational,
        txn_id="txn-visible-before-cutoff",
        episode_id="ep-a",
        created_at=visible_time,
    )
    _add_review_score(
        operational_store.operational,
        txn_id="txn-visible-before-cutoff",
        episode_id="ep-a",
        decision_at=visible_time,
    )
    before = compute_daily_metrics(operational_store.operational, episode_ids=("ep-a",))
    cutoff = "2026-02-10T16:59:59.999999Z"
    alerts_before = compute_alerts(
        before,
        episode_id="ep-a",
        as_of_time=cutoff,
    )
    selected_before = [
        row for row in before if row["episode_id"] == "ep-a" and row["business_date"] == "2026-02-10"
    ]
    review_before = _daily_row(
        before,
        metric_id="model.review_rate",
        episode_id="ep-a",
        business_date="2026-02-10",
    )
    assert review_before["feature"] == "decision_region"
    assert review_before["segment"] == "model_version=test-model"
    assert review_before["value"] == 1.0
    assert review_before["n_window"] == 1
    assert review_before["n_reference"] == 0
    assert review_before["reference_window"] == "not_applicable"

    late_created = datetime(2026, 2, 10, 0, 15, tzinfo=BUSINESS_TZ)
    late_available = datetime(2026, 2, 11, 0, 1, tzinfo=BUSINESS_TZ)
    _add_transaction(
        operational_store.operational,
        txn_id="txn-after-as-of",
        episode_id="ep-a",
        created_at=late_created,
        available_at=late_available,
    )
    _add_review_score(
        operational_store.operational,
        txn_id="txn-after-as-of",
        episode_id="ep-a",
        decision_at=late_created,
        available_at=late_available,
        model_version="late-model",
    )

    after = compute_daily_metrics(operational_store.operational, episode_ids=("ep-a",))
    alerts_after = compute_alerts(
        after,
        episode_id="ep-a",
        as_of_time=cutoff,
    )
    selected_after = [
        row for row in after if row["episode_id"] == "ep-a" and row["business_date"] == "2026-02-10"
    ]
    assert selected_after == selected_before
    assert alerts_after.alerts == alerts_before.alerts


def test_episode_metrics_do_not_include_other_episode_rows(operational_store: StorePair) -> None:
    created_at = datetime(2026, 2, 10, 12, 0, tzinfo=BUSINESS_TZ)
    _add_transaction(
        operational_store.operational,
        txn_id="txn-ep-a",
        episode_id="ep-a",
        created_at=created_at,
        amount=100_000,
    )
    before = compute_daily_metrics(operational_store.operational, episode_ids=("ep-a",))
    selected_before = [
        row for row in before if row["episode_id"] == "ep-a" and row["business_date"] == "2026-02-10"
    ]

    _add_transaction(
        operational_store.operational,
        txn_id="txn-ep-b",
        episode_id="ep-b",
        created_at=created_at,
        amount=99_000_000,
    )
    after = compute_daily_metrics(operational_store.operational, episode_ids=("ep-a",))
    selected_after = [
        row for row in after if row["episode_id"] == "ep-a" and row["business_date"] == "2026-02-10"
    ]
    assert selected_after == selected_before


def test_psi_excludes_transactions_censored_by_p2_observation_flags(
    operational_store: StorePair,
) -> None:
    reference_observed_at = datetime(2025, 11, 15, 9, 0, tzinfo=BUSINESS_TZ)
    _add_customer(
        operational_store.operational,
        customer_id="customer-new-reference",
        episode_id="base",
        observed_at=reference_observed_at,
    )
    current_observed_at = datetime(2026, 2, 10, 9, 0, tzinfo=BUSINESS_TZ)
    _add_customer(
        operational_store.operational,
        customer_id="customer-new-current",
        episode_id="ep-a",
        observed_at=current_observed_at,
    )
    for index in range(35):
        _add_transaction(
            operational_store.operational,
            txn_id=f"txn-new-reference-{index:02d}",
            episode_id="base",
            created_at=reference_observed_at + timedelta(minutes=index + 1),
            amount=100_000 + index,
            customer_id="customer-new-reference",
        )
        _add_transaction(
            operational_store.operational,
            txn_id=f"txn-new-current-{index:02d}",
            episode_id="ep-a",
            created_at=current_observed_at + timedelta(minutes=index + 1),
            amount=100_000 + index,
            customer_id="customer-new-current",
        )

    rows = compute_daily_metrics(operational_store.operational, episode_ids=("ep-a",))
    count_metric = _daily_row(
        rows,
        metric_id="psi.txn_count_24h",
        episode_id="ep-a",
        business_date="2026-02-10",
    )
    amount_metric = _daily_row(
        rows,
        metric_id="psi.amount_avg_7d",
        episode_id="ep-a",
        business_date="2026-02-10",
    )
    for metric in (count_metric, amount_metric):
        assert metric["n_window"] == 0
        assert metric["value"] is None
        assert "window_censored_samples=35" in metric["flags"]
        assert "reference_censored_samples=35" in metric["flags"]
        assert metric["n_reference"] >= 30


def test_smoke_cli_emits_daily_metrics_after_simulator_and_p2_model(
    tmp_path: Path,
) -> None:
    store = create_export_stores(tmp_path / "p3-smoke")
    simulator_config = load_simulator_config(ROOT / "configs" / "simulator.yaml")
    episode_config = load_episode_config(ROOT / "configs" / "episodes.yaml")
    try:
        generated = generate_episodes(
            store.operational,
            store.private,
            simulator_config,
            episode_config,
        )
        store.commit()
        artifact_dir, _ = train_operational_model(
            store.operational,
            run_id="p3-1-smoke",
            data_root=tmp_path / "model-data",
            simulator_config_path=ROOT / "configs" / "simulator.yaml",
        )
        scores = score_monitoring_episodes(
            store.operational,
            model_directory=artifact_dir,
            simulator_config_path=ROOT / "configs" / "simulator.yaml",
        )
        assert scores
        store.commit()
        operational_path = store.operational_path
        private_path = store.private_path
    finally:
        store.close()

    output_path = tmp_path / "daily_metrics.csv"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "monitoring",
            "metrics",
            "--operational-db",
            str(operational_path),
            "--output",
            str(output_path),
        ],
        cwd=ROOT,
        env=os.environ.copy(),
        check=True,
        capture_output=True,
        text=True,
    )
    with output_path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    daily_metrics = []
    for row in rows:
        daily_metrics.append(
            {
                **row,
                "value": float(row["value"]) if row["value"] else None,
                "n_window": int(row["n_window"]),
                "n_reference": int(row["n_reference"]),
                "flags": json.loads(row["flags"]),
            }
        )
    assert result.stderr.startswith("metric_count=")
    assert len(rows) == len(generated.episodes) * 30 * 3
    assert {row["metric_id"] for row in rows} == {
        "psi.txn_count_24h",
        "psi.amount_avg_7d",
        "model.review_rate",
    }
    assert any(int(row["n_reference"]) > 0 for row in rows if row["metric_id"].startswith("psi."))
    assert any(int(row["n_window"]) > 0 for row in rows if row["metric_id"] == "model.review_rate")
    first_day = _daily_row(
        rows,
        metric_id="psi.txn_count_24h",
        episode_id=generated.episodes[0].episode_id,
        business_date="2026-02-01",
    )
    assert json.loads(first_day["flags"]) == ["window_incomplete"] or "window_incomplete" in json.loads(
        first_day["flags"]
    )

    as_of_time = max(row["as_of_time"] for row in daily_metrics)
    alert_results = {}
    for episode in generated.episodes:
        episode_rows = [
            row for row in daily_metrics if row["episode_id"] == episode.episode_id
        ]
        alert_results[episode.episode_id] = compute_alerts(
            episode_rows,
            episode_id=episode.episode_id,
            as_of_time=as_of_time,
        )
    alert_counts = {
        episode_id: len(result.alerts)
        for episode_id, result in alert_results.items()
    }
    print(f"P3.2 smoke alert counts: {alert_counts}")

    incident_results = {
        episode_id: compute_incidents(result.alerts, episode_id=episode_id)
        for episode_id, result in alert_results.items()
    }
    incident_counts = {
        episode_id: result.report["incident_count"]
        for episode_id, result in incident_results.items()
    }
    print(f"P3.3 smoke incident counts: {incident_counts}")
    assert incident_counts == {
        "ep-0001": 2,
        "ep-0002": 2,
        "ep-0003": 2,
        "ep-0004": 2,
        "ep-0005": 1,
    }
    stored_metrics = []
    stored_alerts = []
    stored_incidents = []
    with sqlite3.connect(operational_path) as operational:
        operational.row_factory = sqlite3.Row
        operational.execute("PRAGMA foreign_keys = ON")
        assert persist_monitoring_rows(operational, "daily_metrics", daily_metrics) == 0
        for episode_id, result in alert_results.items():
            persist_monitoring_rows(operational, "alerts", result.alerts)
        for episode_id, result in incident_results.items():
            persist_monitoring_rows(operational, "incidents", result.incidents)
        for episode in generated.episodes:
            stored_metrics.extend(
                read_as_of(
                    operational,
                    "daily_metrics",
                    episode_id=episode.episode_id,
                    t0="2026-01-31T17:00:00.000000Z",
                    as_of_time=as_of_time,
                )
            )
            stored_alerts.extend(
                read_as_of(
                    operational,
                    "alerts",
                    episode_id=episode.episode_id,
                    t0="2026-01-31T17:00:00.000000Z",
                    as_of_time=as_of_time,
                )
            )
            stored_incidents.extend(
                read_as_of(
                    operational,
                    "incidents",
                    episode_id=episode.episode_id,
                    t0="2026-01-31T17:00:00.000000Z",
                    as_of_time=as_of_time,
                )
            )
    for row in stored_metrics:
        row["flags"] = json.loads(row["flags"])
    metric_sort_key = lambda row: (
        row["episode_id"], row["metric_id"], row["as_of_time"], row["feature"], row["segment"]
    )
    assert sorted(stored_metrics, key=metric_sort_key) == sorted(daily_metrics, key=metric_sort_key)
    expected_alerts = [
        alert for result in alert_results.values() for alert in result.alerts
    ]
    assert sorted(stored_alerts, key=lambda row: row["alert_id"]) == sorted(
        expected_alerts, key=lambda row: row["alert_id"]
    )
    expected_incidents = [
        incident for result in incident_results.values() for incident in result.incidents
    ]
    assert sorted(stored_incidents, key=lambda row: (row["incident_id"], row["event_at"])) == sorted(
        expected_incidents, key=lambda row: (row["incident_id"], row["event_at"])
    )
    evaluation_result = evaluate_detection_recall(
        tuple(stored_incidents),
        private_db_path=private_path,
        episode_ids=[episode.episode_id for episode in generated.episodes],
        as_of_time=as_of_time,
    )
    detection_recall = evaluation_result.report["detection_recall"]
    print(f"P3.4 smoke detection recall: {detection_recall}")
    print(
        "P3.4 smoke recall by scenario: "
        f"{evaluation_result.report['by_scenario']}"
    )
    assert detection_recall["detected"] == 2
    assert detection_recall["denominator"] == 2
    assert detection_recall["recall"] == 1.0
    assert evaluation_result.report["incident_counts"]["matched"] == 2
    assert evaluation_result.report["incident_counts"]["ambiguous"] == 0
    assert evaluation_result.report["incident_counts"]["benign_matched"] == 0
    assert evaluation_result.report["incident_counts"]["unmatched"] == 5
    assert evaluation_result.report_path.is_file()
    for episode_id in ("ep-0003", "ep-0005"):
        if alert_counts.get(episode_id) == 5:
            assert incident_counts[episode_id] < alert_counts[episode_id]

    clean_episode = next(item for item in generated.episodes if item.template == "clean")
    clean_result = alert_results[clean_episode.episode_id]
    # With two PSI metrics over 30 daily snapshots, at most five alerts is a
    # deliberately small smoke false-alarm budget; it does not change the
    # monitoring.md threshold if the clean episode exceeds it.
    assert len(clean_result.alerts) <= 5, (
        f"clean smoke generated {len(clean_result.alerts)} alerts; counts={alert_counts}"
    )

    pipeline_episode = next(
        item for item in generated.episodes if item.template == "data_pipeline"
    )
    pipeline_effect = pipeline_episode.incident_summary.effect_available_at
    pipeline_result = alert_results[pipeline_episode.episode_id]
    affected_alerts = [
        alert
        for alert in pipeline_result.alerts
        if alert["feature"] == "amount_avg_7d"
        and alert["as_of_time"] >= pipeline_effect
    ]
    assert affected_alerts, (
        f"data_pipeline smoke has no post-effect amount_avg_7d alert; "
        f"count={len(pipeline_result.alerts)} report={pipeline_result.report}"
    )

    signals_output = tmp_path / "alerts.csv"
    report_output = tmp_path / "signals_report.json"
    signals_cli = subprocess.run(
        [
            sys.executable,
            "-m",
            "monitoring",
            "signals",
            "--operational-db",
            str(operational_path),
            "--episode-id",
            pipeline_episode.episode_id,
            "--as-of-time",
            as_of_time,
            "--output",
            str(signals_output),
            "--report-output",
            str(report_output),
        ],
        cwd=ROOT,
        env=os.environ.copy(),
        check=True,
        capture_output=True,
        text=True,
    )
    with signals_output.open(encoding="utf-8", newline="") as stream:
        cli_alerts = list(csv.DictReader(stream))
    signal_report = json.loads(report_output.read_text(encoding="utf-8"))
    assert signals_cli.stderr.startswith("alert_count=")
    assert len(cli_alerts) == len(pipeline_result.alerts)
    assert signal_report["alert_count"] == len(cli_alerts)
    with sqlite3.connect(operational_path) as operational:
        operational.row_factory = sqlite3.Row
        stored_pipeline_alerts = read_as_of(
            operational,
            "alerts",
            episode_id=pipeline_episode.episode_id,
            t0="2026-01-31T17:00:00.000000Z",
            as_of_time=as_of_time,
        )
    assert sorted(stored_pipeline_alerts, key=lambda row: row["alert_id"]) == sorted(
        pipeline_result.alerts, key=lambda row: row["alert_id"]
    )
