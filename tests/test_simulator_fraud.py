from __future__ import annotations

from copy import deepcopy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from simulator.config import (
    ConfigValidationError,
    SimulatorConfig,
    load_simulator_config,
    validate_simulator_config,
)
from simulator.fraud import CARD_TESTING_METADATA_KEY, FraudSummary, generate_card_testing
from simulator.reproducibility import canonical_content
from simulator.storage import create_export_stores, list_operational_schema
from simulator.transactions import generate_transactions
from simulator.world import generate_world


ROOT = Path(__file__).resolve().parents[1]


def _generate(
    root: Path,
    config: SimulatorConfig,
    *,
    include_fraud: bool,
    fraud_seed: int | None = None,
) -> dict:
    pair = create_export_stores(root)
    try:
        generate_world(pair.operational, config, "smoke")
        generate_transactions(pair.operational, config, "smoke")
        baseline_content = canonical_content(pair.operational)
        summary = None
        metadata = None
        if include_fraud:
            summary = generate_card_testing(
                pair.operational, pair.private, config, seed=fraud_seed
            )
            metadata_row = pair.private.execute(
                "SELECT value_json FROM run_metadata WHERE key = ?",
                (CARD_TESTING_METADATA_KEY,),
            ).fetchone()
            metadata = json.loads(metadata_row[0])
        generated_content = canonical_content(pair.operational)
        operational_schema = list_operational_schema(pair.operational)
        foreign_key_errors = pair.operational.execute("PRAGMA foreign_key_check").fetchall()
        pair.commit()
        return {
            "baseline_content": baseline_content,
            "content": generated_content,
            "metadata": metadata,
            "summary": summary,
            "schema": operational_schema,
            "foreign_key_errors": foreign_key_errors,
        }
    finally:
        pair.close()


def _by_id(rows: list[dict], key: str) -> dict[str, dict]:
    return {row[key]: row for row in rows}


def _parsed_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _without_private_pattern_rows(generated: dict) -> dict:
    metadata = generated["metadata"]
    assert metadata is not None
    transaction_ids = {
        txn_id
        for group in metadata["groups"]
        for txn_id in group["small_transaction_ids"]
    }
    transaction_ids.update(
        group["larger_followup_transaction_id"]
        for group in metadata["groups"]
        if "larger_followup_transaction_id" in group
    )
    event_ids = {
        event["event_id"]
        for event in generated["content"]["transaction_events"]
        if event["txn_id"] in transaction_ids
    }
    return {
        "customers": generated["content"]["customers"],
        "merchants": generated["content"]["merchants"],
        "transactions": [
            row
            for row in generated["content"]["transactions"]
            if row["txn_id"] not in transaction_ids
        ],
        "transaction_events": [
            row
            for row in generated["content"]["transaction_events"]
            if row["event_id"] not in event_ids
        ],
    }


def test_card_testing_signature_and_benign_control_are_observable(tmp_path: Path) -> None:
    config = load_simulator_config("configs/simulator.yaml")
    generated = _generate(
        tmp_path / "card-testing-signature", config, include_fraud=True
    )
    metadata = generated["metadata"]
    summary = generated["summary"]
    assert isinstance(summary, FraudSummary)
    assert metadata is not None
    assert metadata["mechanism"] == "card_testing"
    assert metadata["channel"] == "card"
    assert metadata["seed"] == config.seed.value

    transaction_rows = generated["content"]["transactions"]
    transactions = _by_id(transaction_rows, "txn_id")
    events_by_txn: dict[str, list[dict]] = {}
    for event in generated["content"]["transaction_events"]:
        events_by_txn.setdefault(event["txn_id"], []).append(event)

    # Check that the signature can be found from operational rows alone.
    card_rows_by_customer: dict[str, list[dict]] = {}
    for row in transaction_rows:
        if row["channel"] == "card":
            card_rows_by_customer.setdefault(row["customer_id"], []).append(row)
    observed_card_testing_chain = False
    attempt_count = config.fraud.card_testing.attempts_per_card.value
    for customer_rows in card_rows_by_customer.values():
        ordered_rows = sorted(customer_rows, key=lambda row: row["created_at"])
        for start_index in range(len(ordered_rows) - attempt_count):
            small_sequence = ordered_rows[start_index : start_index + attempt_count]
            start_at = _parsed_timestamp(small_sequence[0]["created_at"])
            end_at = _parsed_timestamp(small_sequence[-1]["created_at"])
            if (
                all(
                    config.fraud.card_testing.minimum_attempt_amount.value
                    <= row["amount"]
                    <= config.fraud.card_testing.maximum_attempt_amount.value
                    for row in small_sequence
                )
                and end_at - start_at
                < timedelta(minutes=config.fraud.card_testing.window_minutes.value)
                and _parsed_timestamp(
                    ordered_rows[start_index + attempt_count]["created_at"]
                )
                > end_at
                and ordered_rows[start_index + attempt_count]["amount"]
                > max(item["amount"] for item in small_sequence)
            ):
                observed_card_testing_chain = True
                break
    assert observed_card_testing_chain

    fraud_group = next(group for group in metadata["groups"] if group["role"] == "fraud")
    small_attempts = [
        transactions[txn_id] for txn_id in fraud_group["small_transaction_ids"]
    ]
    small_attempts.sort(key=lambda row: row["created_at"])
    followup = transactions[fraud_group["larger_followup_transaction_id"]]
    assert len(small_attempts) == config.fraud.card_testing.attempts_per_card.value
    assert {row["channel"] for row in small_attempts + [followup]} == {"card"}
    assert len({row["customer_id"] for row in small_attempts + [followup]}) == 1
    assert all(
        config.fraud.card_testing.minimum_attempt_amount.value
        <= row["amount"]
        <= config.fraud.card_testing.maximum_attempt_amount.value
        for row in small_attempts
    )
    first_attempt = _parsed_timestamp(small_attempts[0]["created_at"])
    last_attempt = _parsed_timestamp(small_attempts[-1]["created_at"])
    followup_time = _parsed_timestamp(followup["created_at"])
    assert last_attempt - first_attempt < timedelta(
        minutes=config.fraud.card_testing.window_minutes.value
    )
    assert followup_time > last_attempt
    assert followup["amount"] > max(row["amount"] for row in small_attempts)
    assert config.fraud.card_testing.minimum_followup_amount.value <= followup["amount"]
    assert followup["amount"] <= config.fraud.card_testing.maximum_followup_amount.value

    fraud_small_approvals = 0
    for row in small_attempts:
        lifecycle = sorted(events_by_txn[row["txn_id"]], key=lambda event: event["event_at"])
        event_types = [event["event_type"] for event in lifecycle]
        assert event_types in (["declined"], ["authorized", "settled"])
        fraud_small_approvals += event_types[0] == "authorized"
    assert fraud_small_approvals == summary.approved_small_attempt_count
    assert len(small_attempts) - fraud_small_approvals == summary.declined_small_attempt_count
    followup_events = sorted(
        events_by_txn[followup["txn_id"]], key=lambda event: event["event_at"]
    )
    assert [event["event_type"] for event in followup_events] in (
        ["declined"],
        ["authorized", "settled"],
    )

    control_group = next(
        group for group in metadata["groups"] if group["role"] == "benign_control"
    )
    control_rows = [transactions[txn_id] for txn_id in control_group["small_transaction_ids"]]
    assert len(control_rows) == len(small_attempts)
    assert {row["channel"] for row in control_rows} == {"card"}
    assert len({row["customer_id"] for row in control_rows}) == 1
    assert all(
        config.fraud.card_testing.minimum_attempt_amount.value
        <= row["amount"]
        <= config.fraud.card_testing.maximum_attempt_amount.value
        for row in control_rows
    )
    control_times = [_parsed_timestamp(row["created_at"]) for row in control_rows]
    assert max(control_times) - min(control_times) < timedelta(
        minutes=config.fraud.card_testing.window_minutes.value
    )
    assert all(
        [
            event["event_type"]
            for event in sorted(
                events_by_txn[row["txn_id"]], key=lambda event: event["event_at"]
            )
        ]
        == ["authorized", "settled"]
        for row in control_rows
    )
    assert summary.benign_control_transaction_count == len(control_rows)
    assert generated["foreign_key_errors"] == []


def test_fraud_metadata_is_private_and_operational_schema_stays_unchanged(tmp_path: Path) -> None:
    config = load_simulator_config("configs/simulator.yaml")
    generated = _generate(
        tmp_path / "private-metadata", config, include_fraud=True
    )

    expected_schema = {
        "episodes",
        "customers",
        "merchants",
        "transactions",
        "transaction_events",
        "label_events",
        "business_calendar",
    }
    assert set(generated["schema"]) == expected_schema
    forbidden = ("truth", "mechanism", "cause", "accepted_actions", "generator", "seed", "injector")
    for table, columns in generated["schema"].items():
        assert not any(part in table.lower() for part in forbidden)
        assert not any(
            part in column.lower()
            for column in columns
            for part in forbidden
        )

    operational_values = [
        value
        for rows in generated["content"].values()
        for row in rows
        for value in row.values()
        if isinstance(value, str)
    ]
    assert not any(
        marker in value.lower()
        for value in operational_values
        for marker in ("card_testing", "benign_control", "fraud")
    )
    assert "role" not in generated["schema"]["transactions"]
    assert "role" not in generated["schema"]["transaction_events"]
    assert "card" in {
        row["channel"] for row in generated["content"]["transactions"]
    }


def test_fraud_rng_is_reproducible_and_does_not_change_base_history(tmp_path: Path) -> None:
    config = load_simulator_config("configs/simulator.yaml")
    baseline = _generate(tmp_path / "baseline-only", config, include_fraud=False)
    first = _generate(
        tmp_path / "fraud-seed-a", config, include_fraud=True, fraud_seed=901
    )
    repeat = _generate(
        tmp_path / "fraud-seed-a-repeat", config, include_fraud=True, fraud_seed=901
    )
    other_fraud_seed = _generate(
        tmp_path / "fraud-seed-b", config, include_fraud=True, fraud_seed=902
    )

    assert first["content"] == repeat["content"]
    assert first["metadata"] == repeat["metadata"]
    assert first["summary"] == repeat["summary"]
    assert first["metadata"] != other_fraud_seed["metadata"]
    assert _without_private_pattern_rows(first) == baseline["content"]
    assert _without_private_pattern_rows(other_fraud_seed) == baseline["content"]
    assert first["baseline_content"] == baseline["content"]
    assert other_fraud_seed["baseline_content"] == baseline["content"]


def test_card_testing_config_rejects_wrong_channel_and_amount_order() -> None:
    with (ROOT / "configs" / "simulator.yaml").open(encoding="utf-8") as stream:
        source_config = yaml.safe_load(stream)

    wrong_channel = deepcopy(source_config)
    wrong_channel["fraud"]["card_testing"]["channel"]["value"] = "qr"
    with pytest.raises(ConfigValidationError, match="channel is fixed to 'card'"):
        validate_simulator_config(wrong_channel)

    non_larger_followup = deepcopy(source_config)
    non_larger_followup["fraud"]["card_testing"]["minimum_followup_amount"]["value"] = 10000
    with pytest.raises(ConfigValidationError, match="must exceed every small attempt"):
        validate_simulator_config(non_larger_followup)
