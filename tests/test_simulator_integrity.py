from __future__ import annotations

import sqlite3

import pytest

from simulator.storage import (
    StorageError,
    insert_operational_record,
    insert_private_record,
    list_operational_schema,
    utc_timestamp,
)


def test_manual_fixture_writes_and_reads_from_separate_exports(p1_2_store) -> None:
    pair = p1_2_store
    assert pair.operational_path != pair.private_path
    assert pair.operational_path.parent.name == "operational"
    assert pair.private_path.parent.name == "private"
    assert pair.operational.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert pair.private.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    assert pair.operational.execute("SELECT count(*) FROM transactions").fetchone()[0] == 5
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
