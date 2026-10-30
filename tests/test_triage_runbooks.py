from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time, timedelta
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from monitoring.incidents import compute_incidents
from monitoring.metrics import compute_daily_metrics
from monitoring.signals import compute_alerts
from monitoring.store import persist_monitoring_rows
from simulator.asof import read_as_of
from simulator.config import load_episode_config, load_simulator_config
from simulator.episodes import generate_episodes
from simulator.storage import create_export_stores, insert_operational_record, utc_timestamp
from triage.gate import GateConfig, evaluate_gate
from triage import runbooks as runbook_module
from triage.runbooks import check_unit_or_null_change, load_triage_config


_T0 = "2026-01-31T17:00:00.000000Z"
_ZONE = ZoneInfo("Asia/Ho_Chi_Minh")
_PRE_DAY = date(2026, 2, 1)
_CHANGE_DAY = date(2026, 2, 2)


def _local_timestamp(day: date, hour: int = 12, minute: int = 0) -> str:
    return utc_timestamp(datetime.combine(day, time(hour, minute), tzinfo=_ZONE))


def _business_end(day: date) -> str:
    return utc_timestamp(datetime.combine(day, time.max, tzinfo=_ZONE))


def _daily_metric(
    episode_id: str,
    as_of_time: str,
    *,
    metric_id: str = "psi.amount_avg_7d",
    feature: str = "amount_avg_7d",
    segment: str = "consumer",
    value: float | None = 0.3,
    reference_window: str = "2025-11-01/2026-01-31",
    n_reference: int = 100,
    window_start: str | None = None,
    window_end: str | None = None,
) -> dict[str, object]:
    business_date = datetime.fromisoformat(as_of_time.replace("Z", "+00:00"))
    business_date = business_date.astimezone(_ZONE).date()
    return {
        "metric_id": metric_id,
        "episode_id": episode_id,
        "as_of_time": as_of_time,
        "event_at": as_of_time,
        "available_at": as_of_time,
        "business_date": business_date.isoformat(),
        "feature": feature,
        "segment": segment,
        "value": value,
        "reference_window": reference_window,
        "window_start": window_start or (business_date - timedelta(days=6)).isoformat(),
        "window_end": window_end or business_date.isoformat(),
        "n_window": 30,
        "n_reference": n_reference,
        "flags": [],
    }


def _alert(
    episode_id: str,
    alert_id: str,
    as_of_time: str,
    *,
    metric: str = "psi.amount_avg_7d",
    feature: str = "amount_avg_7d",
    segment: str = "consumer",
) -> dict[str, object]:
    return {
        "alert_id": alert_id,
        "episode_id": episode_id,
        "as_of_time": as_of_time,
        "metric": metric,
        "feature": feature,
        "segment": segment,
        "value": 0.3,
        "reference": "2025-11-01/2026-01-31",
        "threshold": 0.25,
        "event_at": as_of_time,
        "available_at": as_of_time,
    }


def _incident(episode_id: str, as_of_time: str, alert_ids: list[str]) -> dict[str, object]:
    return {
        "incident_id": f"incident-{episode_id}",
        "parent_incident_id": None,
        "episode_id": episode_id,
        "alert_ids_json": json.dumps(alert_ids, separators=(",", ":")),
        "feature": "amount_avg_7d",
        "segment": "consumer",
        "opened_at": as_of_time,
        "status": "open",
        "event_at": as_of_time,
        "available_at": as_of_time,
    }


def _seed_observations(
    operational,
    *,
    episode_id: str,
    before_amount: int = 10_000,
    after_amount: int | None = 1_000_000,
    as_of_time: str | None = None,
    after_null_count: int = 0,
    before_null_count: int = 0,
    before_count: int = 10,
    after_count: int = 10,
    before_day: date = _PRE_DAY,
    change_day: date = _CHANGE_DAY,
    before_episode_id: str | None = None,
    additional_days: tuple[tuple[date, int | None], ...] = (),
    source_window_start: str | None = None,
    source_window_end: str | None = None,
    segment: str = "consumer",
    extra_episode_rows: bool = False,
    fraud_metric_id: str | None = None,
) -> dict[str, object]:
    as_of_time = as_of_time or _business_end(date(2026, 2, 3))
    insert_operational_record(
        operational, "episodes", {"episode_id": episode_id, "role": "development"}
    )
    insert_operational_record(
        operational,
        "customers",
        {
            "customer_id": f"customer-{episode_id}",
            "episode_id": "base",
            "event_at": _local_timestamp(date(2025, 1, 1)),
            "available_at": _local_timestamp(date(2025, 1, 1)),
            "segment": segment,
            "province": "01",
            "age_band": None,
        },
    )
    insert_operational_record(
        operational,
        "merchants",
        {
            "merchant_id": f"merchant-{episode_id}",
            "episode_id": "base",
            "event_at": _local_timestamp(date(2025, 1, 1)),
            "available_at": _local_timestamp(date(2025, 1, 1)),
            "mcc": "5411",
            "province": "01",
        },
    )

    observations = [
        (before_day, before_amount, before_episode_id or episode_id, before_count, before_null_count),
        (change_day, after_amount, episode_id, after_count, after_null_count),
        *((day, amount, episode_id, 10, 0) for day, amount in additional_days),
    ]
    for day, day_amount, stored_episode, sample_count, null_count in observations:
        for index in range(sample_count):
            amount = day_amount
            if index < null_count:
                amount = None
            event_at = utc_timestamp(
                datetime.combine(day, time(12), tzinfo=_ZONE) + timedelta(minutes=index)
            )
            insert_operational_record(
                operational,
                "transactions",
                {
                    "txn_id": f"txn-{episode_id}-{day.isoformat()}-{index}",
                    "episode_id": stored_episode,
                    "customer_id": f"customer-{episode_id}",
                    "merchant_id": f"merchant-{episode_id}",
                    "created_at": event_at,
                    "event_at": event_at,
                    "available_at": event_at,
                    "amount": amount,
                    "channel": "qr",
                },
            )

    metric = _daily_metric(
        episode_id,
        as_of_time,
        window_start=source_window_start,
        window_end=source_window_end,
    )
    alert = _alert(episode_id, f"alert-{episode_id}", as_of_time)
    insert_operational_record(operational, "daily_metrics", metric)
    insert_operational_record(operational, "alerts", alert)
    alert_ids = [alert["alert_id"]]

    # P3.1's model review rate exists as a raw operational metric but has no reference.
    review_metric = _daily_metric(
        episode_id,
        as_of_time,
        metric_id="model.review_rate",
        feature="decision_region",
        segment="model_version=smoke-v1",
        value=0.2,
        reference_window="not_applicable",
        n_reference=0,
        window_start=source_window_start,
        window_end=source_window_end,
    )
    insert_operational_record(operational, "daily_metrics", review_metric)

    if fraud_metric_id is not None:
        fraud_metric = _daily_metric(
            episode_id,
            as_of_time,
            metric_id=fraud_metric_id,
            feature="outgoing_transfers_24h",
            segment=segment,
            value=1.0,
            reference_window="not_applicable",
            n_reference=0,
            window_start=source_window_start,
            window_end=source_window_end,
        )
        fraud_alert = _alert(
            episode_id,
            f"alert-{episode_id}-fraud",
            as_of_time,
            metric=fraud_metric_id,
            feature="outgoing_transfers_24h",
            segment=segment,
        )
        insert_operational_record(operational, "daily_metrics", fraud_metric)
        insert_operational_record(operational, "alerts", fraud_alert)
        alert_ids.append(str(fraud_alert["alert_id"]))

    incident = _incident(episode_id, as_of_time, alert_ids)
    insert_operational_record(operational, "incidents", incident)

    if extra_episode_rows:
        future_time = _local_timestamp(date(2026, 2, 4))
        insert_operational_record(
            operational,
            "transactions",
            {
                "txn_id": f"txn-{episode_id}-future",
                "episode_id": episode_id,
                "customer_id": f"customer-{episode_id}",
                "merchant_id": f"merchant-{episode_id}",
                "created_at": future_time,
                "event_at": future_time,
                "available_at": future_time,
                "amount": before_amount * 1000,
                "channel": "qr",
            },
        )
    return incident


def _fixture(tmp_path: Path, **kwargs):
    store = create_export_stores(tmp_path / "run")
    incident = _seed_observations(store.operational, episode_id="ep-fixture", **kwargs)
    store.operational.commit()
    return store, incident


def _gate(incident, checklists, *, action="data_ticket", config=None, as_of_time=None):
    raw_config = config or load_triage_config()
    cutoff = as_of_time or incident["available_at"]
    return evaluate_gate(
        incident,
        {
            "cause": "data_pipeline",
            "runbook": "unit_or_null_change",
            "action": action,
            "checklist_results": checklists,
            "reason": "P4.2 runbook fixture.",
        },
        source="rule",
        expected_episode_id=incident["episode_id"],
        as_of_time=cutoff,
        config=GateConfig.from_mapping(raw_config),
    )


@pytest.mark.parametrize("ratio", [100.0, 0.01, 1000.0])
def test_median_ratio_targets_route_data_ticket(tmp_path, ratio: float) -> None:
    before = 1_000_000 if ratio == 0.01 else (1_000 if ratio == 1000 else 10_000)
    after = int(before * ratio)
    store, incident = _fixture(
        tmp_path, before_amount=before, after_amount=after
    )
    try:
        checks = check_unit_or_null_change(store.operational, incident)
        unit = checks["unit_or_null_change"]
        assert unit["status"] == "pass"
        assert unit["scope_match"] is True
        assert unit["window_complete"] is True
        assert unit["details"]["change_detection"]["median_ratio"]["matched_target"] == ratio
        incident_window = unit["details"]["incident_metric_window"]
        after_day = unit["details"]["matched_transition"]["after_start_business_date"]
        assert incident_window["window_start"] <= after_day <= incident_window["window_end"]
        assert _gate(incident, checks)["action"] == "data_ticket"
    finally:
        store.close()


def test_single_transaction_days_block_median_candidate_from_data_ticket(tmp_path) -> None:
    store, incident = _fixture(
        tmp_path,
        before_amount=10_000,
        after_amount=1_000_000,
        before_count=1,
        after_count=1,
    )
    try:
        checks = check_unit_or_null_change(store.operational, incident)
        unit = checks["unit_or_null_change"]
        assert unit["status"] == "pending"
        assert unit["scope_match"] is True
        assert unit["details"]["evaluation_status"] == "insufficient"
        assert unit["details"]["matched_transition"][
            "median_ratio_after_divided_by_before"
        ] == 100
        assert unit["details"]["change_detection"]["small_sample"] == {
            "status": "insufficient",
            "n_min": 2,
            "under_minimum_days": [
                {"business_date": "2026-02-01", "transaction_count": 1},
                {"business_date": "2026-02-02", "transaction_count": 1},
            ],
            "daily_transaction_counts": [
                {"business_date": "2026-02-01", "transaction_count": 1},
                {"business_date": "2026-02-02", "transaction_count": 1},
            ],
        }
        decision = _gate(incident, checks)
        assert decision["action"] == "human_review"
        assert decision["evidence_status"] == "insufficient"
    finally:
        store.close()


def test_dense_median_candidate_still_routes_data_ticket(tmp_path) -> None:
    store, incident = _fixture(
        tmp_path,
        before_amount=10_000,
        after_amount=1_000_000,
        before_count=8,
        after_count=8,
    )
    try:
        checks = check_unit_or_null_change(store.operational, incident)
        unit = checks["unit_or_null_change"]
        assert unit["status"] == "pass"
        assert unit["details"]["change_detection"]["small_sample"]["status"] == "pass"
        assert unit["details"]["matched_transition"]["before_transaction_count"] == 8
        assert unit["details"]["matched_transition"]["after_transaction_count"] == 8
        assert _gate(incident, checks)["action"] == "data_ticket"
    finally:
        store.close()


def test_single_transaction_days_block_null_candidate_from_data_ticket(tmp_path) -> None:
    store, incident = _fixture(
        tmp_path,
        before_amount=10_000,
        after_amount=10_000,
        before_count=1,
        after_count=1,
        after_null_count=1,
    )
    try:
        checks = check_unit_or_null_change(store.operational, incident)
        unit = checks["unit_or_null_change"]
        assert unit["status"] == "pending"
        assert unit["details"]["change_detection"]["null_rate_increase"][
            "status"
        ] == "insufficient"
        assert unit["details"]["change_detection"]["small_sample"][
            "status"
        ] == "insufficient"
        decision = _gate(incident, checks)
        assert decision["action"] == "human_review"
        assert decision["evidence_status"] == "insufficient"
    finally:
        store.close()


@pytest.mark.parametrize(
    ("after", "expected_status"),
    [
        (499_000, "not_applicable"),
        (500_000, "pass"),
        (2_000_000, "pass"),
        (2_001_000, "not_applicable"),
    ],
)
def test_median_ratio_multiplicative_band_includes_exact_boundaries(
    tmp_path, after: int, expected_status: str
) -> None:
    store, incident = _fixture(tmp_path, before_amount=10_000, after_amount=after)
    try:
        result = check_unit_or_null_change(store.operational, incident)["unit_or_null_change"]
        assert result["status"] == expected_status
        median_check = result["details"]["change_detection"]["median_ratio"]
        if expected_status == "pass":
            assert median_check["matched_target"] == 100
            assert median_check["multiplicative_band"] == 2
            assert median_check["accepted_ranges"][0] == {
                "target": 100.0,
                "minimum": 50.0,
                "maximum": 200.0,
            }
        else:
            assert result["scope_match"] is False
    finally:
        store.close()


def test_null_rate_increase_of_at_least_twenty_percentage_points_passes(tmp_path) -> None:
    store, incident = _fixture(
        tmp_path,
        before_amount=10_000,
        after_amount=10_000,
        after_null_count=2,
    )
    try:
        result = check_unit_or_null_change(store.operational, incident)["unit_or_null_change"]
        assert result["status"] == "pass"
        assert result["details"]["matched_transition"]["null_rate_increase"] == pytest.approx(0.2)
        assert result["details"]["change_detection"]["null_rate_increase"]["status"] == "pass"
    finally:
        store.close()


def test_null_rate_exactly_twenty_points_from_nonzero_baseline_passes(tmp_path) -> None:
    store, incident = _fixture(
        tmp_path,
        before_amount=10_000,
        after_amount=10_000,
        before_null_count=1,
        after_null_count=3,
    )
    try:
        result = check_unit_or_null_change(store.operational, incident)["unit_or_null_change"]
        transition = result["details"]["matched_transition"]
        assert result["status"] == "pass"
        assert transition["before_null_rate"] == 0.1
        assert transition["after_null_rate"] == 0.3
        assert transition["null_rate_increase_exact"] == "1/5"
        assert transition["null_rate_threshold_met"] is True
    finally:
        store.close()


def test_null_rate_increase_just_below_twenty_points_does_not_pass(tmp_path) -> None:
    store, incident = _fixture(
        tmp_path,
        before_amount=10_000,
        after_amount=10_000,
        before_null_count=1,
        before_count=10,
        after_null_count=29,
        after_count=100,
    )
    try:
        result = check_unit_or_null_change(store.operational, incident)["unit_or_null_change"]
        comparison = result["details"]["comparisons"][0]
        assert result["status"] == "not_applicable"
        assert comparison["before_null_rate"] == 0.1
        assert comparison["after_null_rate"] == 0.29
        assert comparison["null_rate_increase_exact"] == "19/100"
        assert comparison["null_rate_threshold_met"] is False
    finally:
        store.close()


def test_recovered_historical_change_is_outside_a_new_incident_window(tmp_path) -> None:
    store, incident = _fixture(
        tmp_path,
        before_amount=10_000,
        after_amount=1_000_000,
        additional_days=(
            (date(2026, 2, 3), 10_000),
            (date(2026, 2, 4), 10_000),
            (date(2026, 2, 5), 10_000),
        ),
        as_of_time=_business_end(date(2026, 2, 5)),
        source_window_start="2026-02-04",
        source_window_end="2026-02-05",
    )
    try:
        result = check_unit_or_null_change(store.operational, incident)["unit_or_null_change"]
        assert result["status"] == "not_applicable"
        assert result["scope_match"] is False
        assert result["details"]["incident_metric_window"]["window_start"] == "2026-02-04"
        assert result["details"]["compared_day_pair_count"] == 2
        assert all(
            item["after_start_business_date"] in {"2026-02-04", "2026-02-05"}
            for item in result["details"]["comparisons"]
        )
    finally:
        store.close()


@pytest.mark.parametrize(
    ("before_amount", "after_amount", "before_null_count", "after_null_count", "expected_check"),
    [
        (10_000, 1_000_000, 0, 0, "median_ratio"),
        (10_000, 10_000, 1, 3, "null_rate_increase"),
    ],
)
def test_base_history_at_t0_boundary_contributes_to_change_detection(
    tmp_path,
    before_amount: int,
    after_amount: int,
    before_null_count: int,
    after_null_count: int,
    expected_check: str,
) -> None:
    store, incident = _fixture(
        tmp_path,
        before_amount=before_amount,
        after_amount=after_amount,
        before_null_count=before_null_count,
        after_null_count=after_null_count,
        before_day=date(2026, 1, 31),
        change_day=date(2026, 2, 1),
        before_episode_id="base",
        as_of_time=_business_end(date(2026, 2, 2)),
    )
    try:
        checks = check_unit_or_null_change(store.operational, incident)
        result = checks["unit_or_null_change"]
        assert result["status"] == "pass"
        transition = result["details"]["matched_transition"]
        assert transition["before_last_business_date"] == "2026-01-31"
        assert transition["after_start_business_date"] == "2026-02-01"
        assert result["details"]["change_detection"][expected_check]["status"] == "pass"
        assert _gate(incident, checks)["action"] == "data_ticket"
    finally:
        store.close()


def test_missing_incident_source_metrics_are_insufficient_not_historical_evidence(
    tmp_path, monkeypatch
) -> None:
    store, incident = _fixture(
        tmp_path, before_amount=10_000, after_amount=1_000_000
    )
    original_read_as_of = runbook_module.read_as_of

    def read_without_source_metrics(connection, table, **kwargs):
        rows = original_read_as_of(connection, table, **kwargs)
        return [] if table == "daily_metrics" else rows

    monkeypatch.setattr(runbook_module, "read_as_of", read_without_source_metrics)
    try:
        checks = check_unit_or_null_change(store.operational, incident)
        result = checks["unit_or_null_change"]
        assert result["status"] == "pending"
        assert result["scope_match"] is True
        assert result["window_complete"] is False
        assert result["details"]["evaluation_status"] == "insufficient"
        assert result["details"]["incident_metric_window"]["missing_source_alert_ids"] == [
            "alert-ep-fixture"
        ]
        decision = _gate(incident, checks)
        assert decision["action"] == "human_review"
        assert decision["evidence_status"] == "insufficient"
    finally:
        store.close()


def test_candidate_with_less_than_one_full_post_change_day_is_insufficient(tmp_path) -> None:
    store, incident = _fixture(
        tmp_path,
        before_amount=10_000,
        after_amount=1_000_000,
        as_of_time=_business_end(_CHANGE_DAY),
    )
    try:
        checks = check_unit_or_null_change(store.operational, incident)
        result = checks["unit_or_null_change"]
        assert result["status"] == "pending"
        assert result["window_complete"] is False
        assert result["details"]["evaluation_status"] == "insufficient"
        decision = _gate(incident, checks, action="recheck")
        assert decision["action"] == "recheck"
        assert decision["evidence_status"] == "insufficient"
    finally:
        store.close()


def test_same_segment_allowlisted_fraud_alert_forces_human_review(tmp_path) -> None:
    store, incident = _fixture(
        tmp_path,
        before_amount=10_000,
        after_amount=1_000_000,
        fraud_metric_id="fraud.outgoing_transfers_24h",
    )
    config = deepcopy(load_triage_config())
    outgoing = next(
        item
        for item in config["fraud_side_metrics"]["items"]
        if item["key"] == "outgoing_transfers_24h"
    )
    outgoing["metric_id"] = "fraud.outgoing_transfers_24h"
    try:
        checks = check_unit_or_null_change(store.operational, incident, triage_config=config)
        signal = checks["fraud_signal"]
        assert signal["status"] == "pass"
        assert signal["scope_match"] is True
        decision = _gate(incident, checks, config=config)
        assert decision["action"] == "human_review"
        assert "same segment" in decision["reason"]
    finally:
        store.close()


def test_unavailable_fraud_metrics_are_explicit_and_do_not_block_data_ticket(tmp_path) -> None:
    store, incident = _fixture(tmp_path)
    try:
        checks = check_unit_or_null_change(store.operational, incident)
        signal = checks["fraud_signal"]
        items = signal["details"]["unavailable_metrics"]
        by_key = {item["key"]: item for item in items}
        assert set(by_key) == {
            "outgoing_transfers_24h",
            "first_time_sender",
            "amount_near_10m",
            "model_review_rate",
        }
        assert all(item["status"] == "unavailable" and item["reason"] for item in items)
        assert by_key["model_review_rate"]["metric_id"] == "model.review_rate"
        assert by_key["model_review_rate"]["metric_rows_observed"] == 1
        assert "no reference baseline" in by_key["model_review_rate"]["reason"]
        assert "do not count as evidence that fraud is absent" in signal["details"]["inference_limit"]
        decision = _gate(incident, checks)
        assert decision["action"] == "data_ticket"
        assert decision["evidence_status"] == "sufficient"
    finally:
        store.close()


def test_unallowlisted_fraud_alert_does_not_count_as_a_signal(tmp_path) -> None:
    store, incident = _fixture(
        tmp_path,
        before_amount=10_000,
        after_amount=1_000_000,
        fraud_metric_id="fraud.unlisted_signal",
    )
    try:
        checks = check_unit_or_null_change(store.operational, incident)
        fraud = checks["fraud_signal"]
        assert fraud["status"] == "not_applicable"
        assert "fraud.unlisted_signal" in fraud["details"]["observed_alert_metric_ids"]
        assert _gate(incident, checks)["action"] == "data_ticket"
    finally:
        store.close()


def test_runbook_reads_only_the_incident_episode_and_snapshot(tmp_path) -> None:
    store, incident = _fixture(
        tmp_path,
        before_amount=10_000,
        after_amount=10_000,
        extra_episode_rows=True,
    )
    try:
        insert_operational_record(
            store.operational,
            "episodes",
            {"episode_id": "ep-other", "role": "development"},
        )
        # This other-episode transition is available at the same cutoff.
        other_customer = dict(
            store.operational.execute(
                "SELECT * FROM customers WHERE customer_id = ?",
                ("customer-ep-fixture",),
            ).fetchone()
        )
        insert_operational_record(
            store.operational,
            "customers",
            {
                **other_customer,
                "customer_id": "customer-ep-other",
                "episode_id": "ep-other",
            },
        )
        merchant = dict(
            store.operational.execute(
                "SELECT * FROM merchants WHERE merchant_id = ?",
                ("merchant-ep-fixture",),
            ).fetchone()
        )
        insert_operational_record(
            store.operational,
            "merchants",
            {**merchant, "merchant_id": "merchant-ep-other", "episode_id": "ep-other"},
        )
        other_time = _local_timestamp(_CHANGE_DAY)
        insert_operational_record(
            store.operational,
            "transactions",
            {
                "txn_id": "txn-other-episode",
                "episode_id": "ep-other",
                "customer_id": "customer-ep-other",
                "merchant_id": "merchant-ep-other",
                "created_at": other_time,
                "event_at": other_time,
                "available_at": other_time,
                "amount": 10_000_000,
                "channel": "qr",
            },
        )
        store.operational.commit()

        checks = check_unit_or_null_change(store.operational, incident)
        assert checks["unit_or_null_change"]["status"] == "not_applicable"
        assert checks["unit_or_null_change"]["scope_match"] is False
    finally:
        store.close()


def test_runbook_rejects_cutoff_after_incident_snapshot(tmp_path) -> None:
    store, incident = _fixture(tmp_path)
    try:
        checks = check_unit_or_null_change(
            store.operational,
            incident,
            as_of_time=_business_end(date(2026, 2, 4)),
        )
        assert checks["unit_or_null_change"]["status"] == "error"
        assert checks["fraud_signal"]["status"] == "error"
        assert "must equal the incident snapshot available_at" in checks[
            "unit_or_null_change"
        ]["details"]["error"]
    finally:
        store.close()


def test_smoke_pipeline_incidents_route_and_clean_or_seasonal_do_not_match(tmp_path) -> None:
    store = create_export_stores(tmp_path / "smoke")
    try:
        simulator_config = load_simulator_config()
        episode_config = load_episode_config()
        generated = generate_episodes(
            store.operational,
            store.private,
            simulator_config,
            episode_config,
            profile="smoke",
        )
        episode_ids = tuple(item.episode_id for item in generated.episodes)
        template_by_id = {
            assignment.episode_id: assignment.template
            for assignment in episode_config.profile("smoke").assignments
        }
        pipeline_ids = {
            episode_id
            for episode_id, template in template_by_id.items()
            if template == "data_pipeline"
        }
        non_pipeline_ids = {
            episode_id
            for episode_id, template in template_by_id.items()
            if template in {"clean", "seasonal"}
        }

        metrics = compute_daily_metrics(
            store.operational,
            simulator_config=simulator_config,
            episode_ids=episode_ids,
        )
        persist_monitoring_rows(store.operational, "daily_metrics", metrics)
        final_as_of = max(row["as_of_time"] for row in metrics)
        for episode_id in episode_ids:
            episode_metrics = [row for row in metrics if row["episode_id"] == episode_id]
            alerts = compute_alerts(
                episode_metrics,
                episode_id=episode_id,
                as_of_time=final_as_of,
            ).alerts
            persist_monitoring_rows(store.operational, "alerts", alerts)
            incidents = compute_incidents(alerts, episode_id=episode_id).incidents
            persist_monitoring_rows(store.operational, "incidents", incidents)

        latest_by_id: dict[str, dict[str, object]] = {}
        for episode_id in episode_ids:
            rows = read_as_of(
                store.operational,
                "incidents",
                episode_id=episode_id,
                t0=_T0,
                as_of_time=final_as_of,
            )
            for row in rows:
                prior = latest_by_id.get(row["incident_id"])
                if prior is None or row["event_at"] > prior["event_at"]:
                    latest_by_id[row["incident_id"]] = row

        pipeline_decisions = []
        for incident in latest_by_id.values():
            if incident["episode_id"] not in pipeline_ids:
                continue
            if incident["feature"] != "amount_avg_7d":
                continue
            incident_as_of = str(incident["available_at"])
            checks = check_unit_or_null_change(
                store.operational, incident, as_of_time=incident_as_of
            )
            decision = _gate(incident, checks, as_of_time=incident_as_of)
            pipeline_decisions.append(decision)
            assert checks["unit_or_null_change"]["status"] == "pass"
            assert decision["action"] == "data_ticket"

        assert len(pipeline_decisions) == 2
        for incident in latest_by_id.values():
            if incident["episode_id"] not in non_pipeline_ids:
                continue
            checks = check_unit_or_null_change(store.operational, incident)
            assert checks["unit_or_null_change"]["status"] == "not_applicable"
    finally:
        store.close()
