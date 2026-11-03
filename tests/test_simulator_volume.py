"""Functional contracts for count-scaled templates (docs/data.md section 4, SIM-SCALE)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from simulator.config import load_simulator_config
from simulator.incidents import card_testing_burst_size, inject_fraud_surge, inject_seasonal
from simulator.storage import (
    create_export_stores,
    insert_operational_record,
    insert_private_record,
    utc_timestamp,
)
from simulator.volume import (
    ReferenceWindow,
    VolumeReferenceError,
    injected_transaction_count,
    load_reference_window,
    reference_volume_baselines,
)


ROOT = Path(__file__).resolve().parents[1]
EPISODE = "ep-volume"


def _store(root: Path):
    pair = create_export_stores(root)
    insert_operational_record(pair.operational, "episodes", {"episode_id": EPISODE, "role": "development"})
    insert_private_record(pair.private, "private_episodes", {"episode_id": EPISODE})
    insert_operational_record(
        pair.operational,
        "customers",
        {
            "customer_id": "customer-base",
            "episode_id": "base",
            "event_at": "2026-01-01T00:00:00Z",
            "available_at": "2026-01-01T00:00:00Z",
            "segment": "retail",
            "province": "HCM",
            "age_band": "35-44",
        },
    )
    insert_operational_record(
        pair.operational,
        "merchants",
        {
            "merchant_id": "merchant-base",
            "episode_id": "base",
            "event_at": "2026-01-01T00:00:00Z",
            "available_at": "2026-01-01T00:00:00Z",
            "mcc": "5411",
            "province": "HCM",
        },
    )
    return pair


def _base_transaction(pair, txn_id: str, event_at: str, channel: str, available_at: str | None = None):
    insert_operational_record(
        pair.operational,
        "transactions",
        {
            "txn_id": txn_id,
            "episode_id": "base",
            "customer_id": "customer-base",
            "merchant_id": "merchant-base",
            "created_at": event_at,
            "event_at": event_at,
            "available_at": available_at or event_at,
            "amount": 1000,
            "channel": channel,
        },
    )


def _local_noon(day: date) -> str:
    local = datetime.combine(day, time(12, 0), tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
    return utc_timestamp(local.astimezone(UTC))


def test_reference_window_is_read_from_the_monitoring_contract() -> None:
    assert load_reference_window(ROOT / "configs" / "monitoring.yaml") == ReferenceWindow(
        date(2025, 11, 1), date(2026, 2, 1)
    )


def test_reference_medians_count_only_available_base_rows_inside_the_window(tmp_path: Path) -> None:
    config = load_simulator_config(ROOT / "configs" / "simulator.yaml")
    pair = _store(tmp_path / "baseline")
    try:
        # Window 2025-11-01..2025-11-04 (four local days, zero days included).
        _base_transaction(pair, "txn-d1-qr-1", _local_noon(date(2025, 11, 1)), "qr")
        _base_transaction(pair, "txn-d1-qr-2", _local_noon(date(2025, 11, 1)), "qr")
        _base_transaction(pair, "txn-d1-qr-3", _local_noon(date(2025, 11, 1)), "qr")
        _base_transaction(pair, "txn-d1-card", _local_noon(date(2025, 11, 1)), "card")
        _base_transaction(pair, "txn-d3-qr", _local_noon(date(2025, 11, 3)), "qr")
        _base_transaction(pair, "txn-d3-wallet-1", _local_noon(date(2025, 11, 3)), "wallet")
        _base_transaction(pair, "txn-d3-wallet-2", _local_noon(date(2025, 11, 3)), "wallet")
        # Excluded: outside the window, published after T0, and another episode's row.
        _base_transaction(pair, "txn-before-window", _local_noon(date(2025, 10, 31)), "qr")
        _base_transaction(
            pair,
            "txn-after-t0",
            _local_noon(date(2025, 11, 2)),
            "qr",
            available_at="2026-02-02T00:00:00.000000Z",
        )
        insert_operational_record(
            pair.operational,
            "transactions",
            {
                "txn_id": "txn-episode-row",
                "episode_id": EPISODE,
                "customer_id": "customer-base",
                "merchant_id": "merchant-base",
                "created_at": _local_noon(date(2025, 11, 2)),
                "event_at": _local_noon(date(2025, 11, 2)),
                "available_at": _local_noon(date(2025, 11, 2)),
                "amount": 1000,
                "channel": "qr",
            },
        )

        baselines = reference_volume_baselines(
            pair.operational,
            config,
            ReferenceWindow(date(2025, 11, 1), date(2025, 11, 5)),
        )
    finally:
        pair.operational.close()
        pair.private.close()

    # Daily totals 4, 0, 3, 0 -> median 1.5; qr 3, 0, 1, 0 -> 0.5; wallet 0, 0, 2, 0 -> 0.0.
    assert baselines.total == pytest.approx(1.5)
    assert baselines.channel("qr") == pytest.approx(0.5)
    assert baselines.channel("wallet") == pytest.approx(0.0)
    assert baselines.channel("card") == pytest.approx(0.0)


def test_injected_count_keeps_the_smoke_floor_and_scales_with_the_reference() -> None:
    # Smoke scale (median total 4, qr 1): the floors decide.
    assert injected_transaction_count(minimum=20, relative_amplitude=1.0, baseline=1.0) == 20
    assert injected_transaction_count(minimum=9, relative_amplitude=0.5, baseline=4.0) == 9
    # Pilot scale (median total 174, qr 74): the amplitudes decide.
    assert injected_transaction_count(minimum=9, relative_amplitude=0.5, baseline=174.0) == 87
    assert injected_transaction_count(minimum=20, relative_amplitude=1.0, baseline=74.0) == 74
    with pytest.raises(VolumeReferenceError):
        injected_transaction_count(minimum=9, relative_amplitude=0.5, baseline=0.0)


def test_seasonal_calendar_expectation_is_the_injected_change_on_its_channel(tmp_path: Path) -> None:
    config = load_simulator_config(ROOT / "configs" / "simulator.yaml")
    pair = _store(tmp_path / "seasonal")
    try:
        result = inject_seasonal(
            pair.operational,
            pair.private,
            config,
            episode_id=EPISODE,
            incident_truth_id="truth-volume-seasonal",
            business_date=date(2026, 2, 5),
            event_name="Synthetic promotion event",
            additional_transaction_count=74,
            calendar_scope_baseline=74.0,
            seed=17,
        )
        calendar = pair.operational.execute(
            "SELECT expected_effect_json, scope_json FROM business_calendar WHERE episode_id = ?",
            (EPISODE,),
        ).fetchone()
        channels = [
            row[0]
            for row in pair.operational.execute(
                "SELECT channel FROM transactions WHERE txn_id IN ("
                + ", ".join("?" for _ in result.affected_transaction_ids)
                + ")",
                result.affected_transaction_ids,
            )
        ]
    finally:
        pair.operational.close()
        pair.private.close()

    assert len(result.affected_transaction_ids) == 74
    assert set(channels) == {config.transactions.channel.value}
    assert json.loads(calendar[0]) == {
        "txn_count": {"aggregation": "sum", "relative_change": 74 / 74.0}
    }
    assert json.loads(calendar[1]) == {"channel": [config.transactions.channel.value]}


def test_card_burst_at_pilot_count_keeps_its_window_and_publishes_a_neutral_qr_calendar(
    tmp_path: Path,
) -> None:
    config = load_simulator_config(ROOT / "configs" / "simulator.yaml")
    smoke_pair = _store(tmp_path / "burst-smoke")
    pilot_pair = _store(tmp_path / "burst-pilot")
    try:
        smoke = inject_fraud_surge(
            smoke_pair.operational,
            smoke_pair.private,
            config,
            episode_id=EPISODE,
            incident_truth_id="truth-volume-burst-smoke",
            business_date=date(2026, 2, 5),
            seed=23,
        )
        pilot = inject_fraud_surge(
            pilot_pair.operational,
            pilot_pair.private,
            config,
            episode_id=EPISODE,
            incident_truth_id="truth-volume-burst-pilot",
            business_date=date(2026, 2, 5),
            seed=23,
            injected_transaction_count=87,
            calendar_event_name="Synthetic promotion event",
            calendar_channel=config.transactions.channel.value,
            calendar_scope_baseline=74.0,
        )
        rows = [
            dict(row)
            for row in pilot_pair.operational.execute(
                "SELECT txn_id, customer_id, channel, created_at FROM transactions WHERE txn_id IN ("
                + ", ".join("?" for _ in pilot.affected_transaction_ids)
                + ")",
                pilot.affected_transaction_ids,
            )
        ]
        calendar = pilot_pair.operational.execute(
            "SELECT expected_effect_json FROM business_calendar WHERE episode_id = ?",
            (EPISODE,),
        ).fetchone()
    finally:
        for pair in (smoke_pair, pilot_pair):
            pair.operational.close()
            pair.private.close()

    assert len(smoke.affected_transaction_ids) == card_testing_burst_size(config) == 9
    assert len(pilot.affected_transaction_ids) == 87
    assert {row["channel"] for row in rows} == {"card"}
    assert len({row["customer_id"] for row in rows}) == 1
    created = sorted(
        datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")) for row in rows
    )
    # Dense attempts stay in the configured 10-minute window; the follow-up adds at most ~105 s.
    assert created[-1] - created[0] <= timedelta(minutes=10, seconds=60 + 45)
    # Cards are not the qr scope, so the published expectation is neutral.
    assert json.loads(calendar[0]) == {"txn_count": {"aggregation": "sum", "relative_change": 0.0}}
