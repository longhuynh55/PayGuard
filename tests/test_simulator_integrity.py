from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from simulator.config import load_simulator_config
from simulator.storage import (
    StorageError,
    create_export_stores,
    insert_operational_record,
    insert_private_record,
    list_operational_schema,
    utc_timestamp,
)
from simulator.transactions import generate_transactions
from simulator.world import generate_world


def test_manual_fixture_writes_and_reads_from_separate_exports(p1_2_store) -> None:
    pair = p1_2_store
    assert pair.operational_path != pair.private_path
    assert pair.operational_path.parent.name == "operational"
    assert pair.private_path.parent.name == "private"
    assert pair.operational.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert pair.private.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    assert pair.operational.execute("SELECT count(*) FROM transactions").fetchone()[0] == 6
    insert_private_record(pair.private, "private_episodes", {"episode_id": "ep-a"})
    insert_private_record(
        pair.private,
        "run_metadata",
        {"key": "seed", "value_json": {"seed": 17}},
    )
    insert_private_record(
        pair.private,
        "incident_truth",
        {
            "incident_truth_id": "truth-a",
            "episode_id": "ep-a",
            "cause": "fraud_surge",
            "requires_intervention": 1,
            "mechanism": "card_testing",
            "rule_covered": 1,
            "affected_entities_json": ["merchant-a"],
            "affected_segments_json": ["retail"],
            "affected_features_json": ["txn_count"],
            "effect_start_at": "2026-02-01T00:00:00Z",
            "effect_end_at": None,
            "effect_available_at": "2026-02-01T00:00:00Z",
            "intervention_start_at": "2026-02-01T00:00:00Z",
            "deadline_at": "2026-02-08T00:00:00Z",
            "accepted_actions_json": ["fraud_ops", "human_review"],
            "parent_scenario": None,
        },
    )
    pair.private.commit()
    assert pair.private.execute("SELECT count(*) FROM incident_truth").fetchone()[0] == 1


def test_duplicate_primary_key_and_orphan_foreign_keys_fail(p1_2_store) -> None:
    pair = p1_2_store
    customer = dict(pair.operational.execute("SELECT * FROM customers WHERE customer_id = 'customer-base'").fetchone())
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE|PRIMARY KEY"):
        insert_operational_record(pair.operational, "customers", customer)

    orphan = {
        "txn_id": "txn-orphan",
        "episode_id": "ep-a",
        "customer_id": "missing-customer",
        "merchant_id": "merchant-base",
        "created_at": "2026-02-03T00:00:00Z",
        "event_at": "2026-02-03T00:00:00Z",
        "available_at": "2026-02-03T00:00:00Z",
        "amount": 100,
        "channel": "qr",
    }
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        insert_operational_record(pair.operational, "transactions", orphan)

    cross_episode = {**orphan, "txn_id": "txn-cross-episode", "customer_id": "customer-b"}
    with pytest.raises(sqlite3.IntegrityError, match="same episode"):
        insert_operational_record(pair.operational, "transactions", cross_episode)

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        insert_operational_record(
            pair.operational,
            "label_events",
            {
                "event_id": "label-orphan",
                "txn_id": "missing-transaction",
                "episode_id": "ep-a",
                "event_at": "2026-02-03T00:00:00Z",
                "label": "fraud",
                "label_status": "confirmed",
                "reported_at": "2026-02-03T00:00:00Z",
                "available_at": "2026-02-03T00:00:00Z",
                "source": "investigator",
                "supersedes_event_id": None,
            },
        )


def test_customer_primary_key_rejects_null_and_missing_values(p1_2_store) -> None:
    customer = dict(
        p1_2_store.operational.execute(
            "SELECT * FROM customers WHERE customer_id = 'customer-base'"
        ).fetchone()
    )
    null_key = {**customer, "customer_id": None}
    for _ in range(2):
        with pytest.raises(sqlite3.IntegrityError, match="NOT NULL constraint failed: customers.customer_id"):
            insert_operational_record(p1_2_store.operational, "customers", null_key)

    missing_key = dict(customer)
    del missing_key["customer_id"]
    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL constraint failed: customers.customer_id"):
        insert_operational_record(p1_2_store.operational, "customers", missing_key)


def test_primary_keys_are_explicitly_not_null_and_missing_keys_fail(p1_2_store) -> None:
    for connection in (p1_2_store.operational, p1_2_store.private):
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        for (table,) in tables:
            pk_columns = [
                row
                for row in connection.execute(f'PRAGMA table_info("{table}")')
                if row[5]
            ]
            assert pk_columns, table
            assert all(row[3] == 1 for row in pk_columns), table

    for row in ({"role": "development"}, {"episode_id": None, "role": "development"}):
        with pytest.raises(sqlite3.IntegrityError, match="NOT NULL constraint failed"):
            insert_operational_record(p1_2_store.operational, "episodes", row)

    for row in ({"value_json": {}}, {"key": None, "value_json": {}}):
        with pytest.raises(sqlite3.IntegrityError, match="NOT NULL constraint failed"):
            insert_private_record(p1_2_store.private, "run_metadata", row)


def test_episode_relations_cannot_be_broken_by_update(p1_2_store) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        p1_2_store.operational.execute(
            """
            UPDATE transactions
            SET customer_id = 'customer-b', merchant_id = 'merchant-b'
            WHERE txn_id = 'txn-a-at-availability-cutoff'
            """
        )
    with pytest.raises(sqlite3.IntegrityError, match="customer identity and episode are immutable"):
        p1_2_store.operational.execute(
            "UPDATE customers SET episode_id = 'ep-b' WHERE customer_id = 'customer-base'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="merchant identity and episode are immutable"):
        p1_2_store.operational.execute(
            "UPDATE merchants SET episode_id = 'ep-b' WHERE merchant_id = 'merchant-base'"
        )
    assert p1_2_store.operational.execute("PRAGMA foreign_key_check").fetchall() == []


def test_transaction_event_episode_boundary_and_parent_rules(p1_2_store) -> None:
    invalid_events = [
        {
            "event_id": "base-event-at-t0",
            "txn_id": "txn-base-at-boundary",
            "episode_id": "base",
            "event_type": "authorized",
            "event_at": "2026-01-31T17:00:00Z",
            "available_at": "2026-01-31T17:00:00Z",
            "response_code": "00",
        },
        {
            "event_id": "episode-event-before-t0",
            "txn_id": "txn-base-at-boundary",
            "episode_id": "ep-a",
            "event_type": "authorized",
            "event_at": "2026-01-31T16:59:59.999999Z",
            "available_at": "2026-01-31T17:00:00Z",
            "response_code": "00",
        },
    ]
    for event in invalid_events:
        with pytest.raises(sqlite3.IntegrityError, match="relative to T0"):
            insert_operational_record(
                p1_2_store.operational, "transaction_events", event
            )

    event_for_other_episode = {
        "event_id": "episode-a-event-pointing-to-episode-b-transaction",
        "txn_id": "txn-b-other-episode",
        "episode_id": "ep-a",
        "event_type": "authorized",
        "event_at": "2026-01-31T17:00:01Z",
        "available_at": "2026-01-31T17:00:01Z",
        "response_code": "00",
    }
    with pytest.raises(sqlite3.IntegrityError, match="base or same-episode transaction"):
        insert_operational_record(
            p1_2_store.operational, "transaction_events", event_for_other_episode
        )


def test_transaction_events_are_append_only(p1_2_store) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        p1_2_store.operational.execute(
            "UPDATE transaction_events SET event_type = 'settled' WHERE event_id = 'txn-base-authorized'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        p1_2_store.operational.execute(
            "DELETE FROM transaction_events WHERE event_id = 'txn-base-authorized'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="event ids cannot be replaced"):
        p1_2_store.operational.execute(
            """
            INSERT OR REPLACE INTO transaction_events
                (event_id, txn_id, episode_id, event_type, event_at, available_at, response_code)
            VALUES
                ('txn-base-authorized', 'txn-base-before-t0', 'base', 'settled',
                 '2026-01-30T10:20:00.000000Z', '2026-01-30T10:20:00.000000Z', NULL)
            """
        )


def test_schema_rejects_malformed_timestamp_and_mismatched_label_time(p1_2_store) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        p1_2_store.operational.execute(
            """
            INSERT INTO customers
                (customer_id, episode_id, event_at, available_at, segment, province, age_band)
            VALUES ('customer-bad-time', 'base', 'not-utc', '2026-02-01T00:00:00.000000Z', 'retail', 'HCM', NULL)
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="label event_at must match"):
        insert_operational_record(
            p1_2_store.operational,
            "label_events",
            {
                "event_id": "label-wrong-event-time",
                "txn_id": "txn-base-before-t0",
                "episode_id": "base",
                "event_at": "2026-01-30T10:00:01Z",
                "label": "legit",
                "label_status": "confirmed",
                "reported_at": "2026-02-01T00:00:00Z",
                "available_at": "2026-02-01T00:00:00Z",
                "source": "customer_report",
                "supersedes_event_id": None,
            },
        )


def test_operational_catalog_has_no_truth_or_generator_columns(p1_2_store) -> None:
    schema = list_operational_schema(p1_2_store.operational)
    assert set(schema) == {
        "episodes",
        "customers",
        "merchants",
        "transactions",
        "transaction_events",
        "label_events",
        "business_calendar",
        "model_scores",
    }
    forbidden_parts = ("truth", "mechanism", "cause", "accepted_actions", "generator", "seed", "injector")
    for table, columns in schema.items():
        assert not any(part in table.lower() for part in forbidden_parts)
        assert not any(
            part in column.lower()
            for column in columns
            for part in forbidden_parts
        )
    assert "incident_truth" not in schema
    assert "incident_truth" in {
        row[0]
        for row in p1_2_store.private.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def test_timestamp_writer_normalizes_utc_and_rejects_naive_values() -> None:
    assert utc_timestamp("2026-02-01T00:00:00+07:00") == "2026-01-31T17:00:00.000000Z"
    with pytest.raises(StorageError, match="naive timestamps"):
        utc_timestamp("2026-02-01T00:00:00")


def test_private_writer_cannot_target_operational_table(p1_2_store) -> None:
    with pytest.raises(StorageError, match="unknown private table"):
        insert_private_record(p1_2_store.private, "transactions", {})


def test_smoke_generation_integrity_and_observable_daily_hourly_rhythm(tmp_path: Path) -> None:
    config = load_simulator_config("configs/simulator.yaml")
    pair = create_export_stores(tmp_path / "p1_3-smoke")
    try:
        world = generate_world(pair.operational, config, "smoke")
        summary = generate_transactions(pair.operational, config, "smoke")
        pair.commit()

        assert len(world.customer_ids) == config.scale_for("smoke").customer_count.value
        assert len(world.merchant_ids) == config.scale_for("smoke").merchant_count.value
        assert summary.transaction_count > 0
        assert summary.lifecycle_event_count == summary.transaction_count * 2

        customers = [dict(row) for row in pair.operational.execute("SELECT * FROM customers")]
        merchants = [dict(row) for row in pair.operational.execute("SELECT * FROM merchants")]
        transactions = [dict(row) for row in pair.operational.execute("SELECT * FROM transactions")]
        events = [dict(row) for row in pair.operational.execute("SELECT * FROM transaction_events")]
        assert {row["episode_id"] for row in customers + merchants + transactions + events} == {"base"}
        assert {row["customer_id"] for row in transactions} <= set(world.customer_ids)
        assert {row["merchant_id"] for row in transactions} <= set(world.merchant_ids)
        assert all(row["segment"] in config.world.customer_segments.value for row in customers)
        assert all(row["province"] in config.world.customer_provinces.value for row in customers)
        assert all(row["age_band"] in config.world.customer_age_bands.value for row in customers)
        assert all(row["mcc"] in config.world.merchant_mcc_codes.value for row in merchants)
        assert all(row["province"] in config.world.merchant_provinces.value for row in merchants)
        assert pair.operational.execute("PRAGMA foreign_key_check").fetchall() == []

        local_tz = ZoneInfo(config.timeline.business_timezone.value)
        start = config.timeline.start_business_date.value
        t0 = config.timeline.t0_business_date.value
        t0_utc = datetime.combine(t0, time.min).replace(tzinfo=local_tz).astimezone(UTC)
        for entity in customers + merchants:
            event_at = datetime.fromisoformat(entity["event_at"].replace("Z", "+00:00"))
            available_at = datetime.fromisoformat(entity["available_at"].replace("Z", "+00:00"))
            assert available_at >= event_at
            assert available_at == event_at
            assert event_at < t0_utc
            assert event_at.astimezone(local_tz).date() >= start

        txn_ids = set()
        transactions_by_date: dict[str, int] = {}
        hour_counts: dict[int, int] = {}
        for row in transactions:
            txn_ids.add(row["txn_id"])
            assert row["event_at"] == row["created_at"]
            assert row["available_at"] == row["created_at"]
            assert type(row["amount"]) is int
            assert config.transactions.minimum_amount.value <= row["amount"] <= config.transactions.maximum_amount.value
            assert row["channel"] == config.transactions.channel.value
            created_at = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
            local_created_at = created_at.astimezone(local_tz)
            assert start <= local_created_at.date() < t0
            transactions_by_date[local_created_at.date().isoformat()] = (
                transactions_by_date.get(local_created_at.date().isoformat(), 0) + 1
            )
            hour_counts[local_created_at.hour] = hour_counts.get(local_created_at.hour, 0) + 1

        events_by_transaction: dict[str, list[dict]] = {}
        for event in events:
            events_by_transaction.setdefault(event["txn_id"], []).append(event)
            event_at = datetime.fromisoformat(event["event_at"].replace("Z", "+00:00"))
            available_at = datetime.fromisoformat(event["available_at"].replace("Z", "+00:00"))
            assert available_at >= event_at
            assert event_at < t0_utc
        assert set(events_by_transaction) == txn_ids
        for row in transactions:
            created_at = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
            lifecycle = sorted(events_by_transaction[row["txn_id"]], key=lambda event: event["event_at"])
            assert [event["event_type"] for event in lifecycle] == ["authorized", "settled"]
            authorized_at = created_at + timedelta(
                seconds=config.transactions.authorization_delay_seconds.value
            )
            settled_at = authorized_at + timedelta(
                minutes=config.transactions.settlement_delay_minutes.value
            )
            assert lifecycle[0]["event_at"] == utc_timestamp(authorized_at)
            assert lifecycle[0]["response_code"] == config.transactions.authorization_response_code.value
            assert lifecycle[0]["available_at"] == lifecycle[0]["event_at"]
            assert lifecycle[1]["event_at"] == utc_timestamp(settled_at)
            assert lifecycle[1]["available_at"] == lifecycle[1]["event_at"]

        expected_count = (
            len(world.customer_ids)
            * (t0 - start).days
            * config.world.expected_transactions_per_customer_local_day.value
        )
        assert expected_count * 0.75 < len(transactions) < expected_count * 1.25
        assert len(transactions_by_date) > 150
        assert len(set(transactions_by_date.values())) > 1
        peak_hours = sum(hour_counts.get(hour, 0) for hour in (11, 12, 13, 17, 18, 19, 20))
        quiet_hours = sum(hour_counts.get(hour, 0) for hour in range(6))
        assert peak_hours > quiet_hours * 4
    finally:
        pair.close()
