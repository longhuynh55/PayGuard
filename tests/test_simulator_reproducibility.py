from __future__ import annotations

import json
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from simulator.config import SimulatorConfig, load_simulator_config
from simulator.reproducibility import (
    FINGERPRINT_METADATA_KEY,
    canonical_content,
    canonical_fingerprint,
    record_canonical_fingerprint,
)
from simulator.storage import create_export_stores
from simulator.transactions import generate_transactions
from simulator.world import generate_world


def _generate(
    root: Path,
    config: SimulatorConfig,
    *,
    world_seed: int | None = None,
    transaction_seed: int | None = None,
) -> dict:
    pair = create_export_stores(root)
    try:
        world = generate_world(pair.operational, config, "smoke", seed=world_seed)
        transactions = generate_transactions(
            pair.operational,
            config,
            "smoke",
            seed=transaction_seed,
            world_summary=world,
        )
        fingerprint = canonical_fingerprint(pair.operational)
        stored_fingerprint = record_canonical_fingerprint(pair.operational, pair.private)
        pair.commit()
        metadata_row = pair.private.execute(
            "SELECT value_json FROM run_metadata WHERE key = ?",
            (FINGERPRINT_METADATA_KEY,),
        ).fetchone()
        return {
            "content": canonical_content(pair.operational),
            "fingerprint": fingerprint,
            "stored_fingerprint": stored_fingerprint,
            "metadata": json.loads(metadata_row[0]),
            "world": world,
            "transactions": transactions,
            "foreign_key_errors": pair.operational.execute("PRAGMA foreign_key_check").fetchall(),
        }
    finally:
        pair.close()


def _assert_generated_invariants(result: dict, config: SimulatorConfig) -> None:
    content = result["content"]
    customers = content["customers"]
    merchants = content["merchants"]
    transactions = content["transactions"]
    events = content["transaction_events"]
    assert result["foreign_key_errors"] == []
    assert len(customers) == config.scale_for("smoke").customer_count.value
    assert len(merchants) == config.scale_for("smoke").merchant_count.value
    assert transactions
    assert len(events) == result["transactions"].lifecycle_event_count
    assert all(row["episode_id"] == "base" for row in customers + merchants + transactions + events)
    customer_ids = {row["customer_id"] for row in customers}
    merchant_ids = {row["merchant_id"] for row in merchants}
    transactions_by_id = {row["txn_id"]: row for row in transactions}
    assert all(row["customer_id"] in customer_ids for row in transactions)
    assert all(
        (row["merchant_id"] is None) != (row["recipient_customer_id"] is None)
        for row in transactions
    )
    assert all(
        row["merchant_id"] in merchant_ids
        for row in transactions
        if row["merchant_id"] is not None
    )
    assert all(
        type(row["amount"]) is int
        and config.transactions.minimum_amount.value <= row["amount"] <= config.transactions.maximum_amount.value
        for row in transactions
    )

    events_by_txn: dict[str, list[dict]] = {}
    for event in events:
        events_by_txn.setdefault(event["txn_id"], []).append(event)
    t0 = datetime.combine(
        config.timeline.t0_business_date.value,
        time.min,
        tzinfo=ZoneInfo(config.timeline.business_timezone.value),
    ).astimezone(UTC)
    start_date = config.timeline.start_business_date.value
    t0_date = config.timeline.t0_business_date.value
    local_tz = ZoneInfo(config.timeline.business_timezone.value)
    for txn_id, transaction in transactions_by_id.items():
        created_at = datetime.fromisoformat(transaction["created_at"].replace("Z", "+00:00"))
        assert transaction["event_at"] == transaction["created_at"]
        assert transaction["available_at"] == transaction["created_at"]
        assert transaction["channel"] in config.transactions.channel_weights.value
        if transaction["recipient_customer_id"] is not None:
            assert transaction["recipient_customer_id"] in customer_ids
            assert transaction["recipient_customer_id"] != transaction["customer_id"]
            assert transaction["channel"] == config.transactions.account_transfer_channel.value
        assert start_date <= created_at.astimezone(local_tz).date() < t0_date
        lifecycle = sorted(events_by_txn[txn_id], key=lambda event: event["event_at"])
        event_types = [event["event_type"] for event in lifecycle]
        assert event_types[:2] == ["authorized", "settled"]
        assert len(event_types) in (2, 3)
        if len(event_types) == 3:
            assert event_types[2] in {"refunded", "reversed"}
            assert 0 < lifecycle[2]["amount"] <= transaction["amount"]
            assert lifecycle[2]["event_at"] > transaction["created_at"]
        else:
            assert all(event["amount"] is None for event in lifecycle)
        assert all(event["available_at"] == event["event_at"] for event in lifecycle)
        assert all(
            datetime.fromisoformat(event["event_at"].replace("Z", "+00:00")) < t0
            for event in lifecycle
        )
        assert lifecycle[0]["event_at"] == (
            created_at
            + timedelta(seconds=config.transactions.authorization_delay_seconds.value)
        ).isoformat(timespec="microseconds").replace("+00:00", "Z")
        expected_settlement = created_at + timedelta(
            seconds=config.transactions.authorization_delay_seconds.value,
            minutes=config.transactions.settlement_delay_minutes.value,
        )
        assert lifecycle[0]["response_code"] == config.transactions.authorization_response_code.value
        assert lifecycle[1]["event_at"] == (
            expected_settlement.isoformat(timespec="microseconds").replace("+00:00", "Z")
        )


def test_same_seed_and_config_have_the_same_canonical_content(tmp_path: Path) -> None:
    config = load_simulator_config("configs/simulator.yaml")
    first = _generate(tmp_path / "first", config)
    second = _generate(tmp_path / "second", config)

    assert first["content"] == second["content"]
    assert first["fingerprint"] == second["fingerprint"]
    assert first["stored_fingerprint"] == first["fingerprint"]
    assert second["stored_fingerprint"] == second["fingerprint"]
    assert first["metadata"] == second["metadata"]
    assert first["metadata"]["algorithm"] == "sha256"
    assert first["metadata"]["tables"] == sorted(first["content"])


def test_changing_the_seed_changes_data_while_preserving_invariants(tmp_path: Path) -> None:
    config = load_simulator_config("configs/simulator.yaml")
    first = _generate(tmp_path / "seed-a", config, world_seed=101, transaction_seed=202)
    second = _generate(tmp_path / "seed-b", config, world_seed=303, transaction_seed=404)

    assert first["content"] != second["content"]
    assert first["fingerprint"] != second["fingerprint"]
    _assert_generated_invariants(first, config)
    _assert_generated_invariants(second, config)


def test_world_and_transaction_random_streams_are_independent(tmp_path: Path) -> None:
    config = load_simulator_config("configs/simulator.yaml")
    changed_world_seed = _generate(
        tmp_path / "world-seed-a", config, world_seed=71, transaction_seed=808
    )
    other_world_seed = _generate(
        tmp_path / "world-seed-b", config, world_seed=72, transaction_seed=808
    )
    assert changed_world_seed["content"]["customers"] != other_world_seed["content"]["customers"]
    assert changed_world_seed["content"]["merchants"] != other_world_seed["content"]["merchants"]
    assert changed_world_seed["world"].account_roles != other_world_seed["world"].account_roles
    first_merchant_payments = {
        row["txn_id"]: row
        for row in changed_world_seed["content"]["transactions"]
        if row["merchant_id"] is not None
    }
    second_merchant_payments = {
        row["txn_id"]: row
        for row in other_world_seed["content"]["transactions"]
        if row["merchant_id"] is not None
    }
    assert first_merchant_payments.keys() == second_merchant_payments.keys()
    independent_columns = (
        "customer_id",
        "created_at",
        "event_at",
        "available_at",
        "amount",
        "channel",
    )
    for txn_id, first_row in first_merchant_payments.items():
        assert {
            column: first_row[column] for column in independent_columns
        } == {
            column: second_merchant_payments[txn_id][column]
            for column in independent_columns
        }
    first_events = {
        event["event_id"]: event
        for event in changed_world_seed["content"]["transaction_events"]
        if event["txn_id"] in first_merchant_payments
    }
    second_events = {
        event["event_id"]: event
        for event in other_world_seed["content"]["transaction_events"]
        if event["txn_id"] in second_merchant_payments
    }
    assert first_events == second_events

    changed_transaction_seed = _generate(
        tmp_path / "transaction-seed-b", config, world_seed=71, transaction_seed=809
    )
    assert changed_world_seed["content"]["customers"] == changed_transaction_seed["content"]["customers"]
    assert changed_world_seed["content"]["merchants"] == changed_transaction_seed["content"]["merchants"]
    assert changed_world_seed["content"]["transactions"] != changed_transaction_seed["content"]["transactions"]
    _assert_generated_invariants(changed_world_seed, config)
    _assert_generated_invariants(other_world_seed, config)
    _assert_generated_invariants(changed_transaction_seed, config)


def test_content_fingerprint_uses_primary_key_sorted_rows(tmp_path: Path) -> None:
    config = load_simulator_config("configs/simulator.yaml")
    generated = _generate(tmp_path / "canonical", config)
    primary_keys = {
        "customers": "customer_id",
        "merchants": "merchant_id",
        "transactions": "txn_id",
        "transaction_events": "event_id",
    }
    for table, key in primary_keys.items():
        assert [row[key] for row in generated["content"][table]] == sorted(
            row[key] for row in generated["content"][table]
        )
    assert generated["fingerprint"] == canonical_fingerprint_from_content(generated["content"])


def canonical_fingerprint_from_content(content: dict) -> str:
    """Recompute the contract digest from already key-sorted canonical rows."""
    import hashlib

    payload = json.dumps(
        content,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
