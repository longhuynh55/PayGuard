from __future__ import annotations

from copy import deepcopy
import json
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

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


def _model_cohorts_from_docs() -> dict[str, tuple[date, date]]:
    model_doc = (ROOT / "docs" / "model.md").read_text(encoding="utf-8")
    cohorts: dict[str, tuple[date, date]] = {}
    for line in model_doc.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        label = cells[0]
        if label == "Fit model":
            cohort_name = "fit"
        elif label.startswith("Validation "):
            cohort_name = "validation"
        elif label.startswith("Calibration:"):
            cohort_name = "calibration"
        else:
            continue
        bounds = re.search(r"\[([^,]+),\s*([^\)]+)\)", cells[1])
        assert bounds is not None, f"missing split bounds for {cohort_name} in docs/model.md"
        cohorts[cohort_name] = tuple(
            datetime.strptime(bound.strip(), "%d/%m/%Y").date()
            for bound in bounds.groups()
        )
    assert set(cohorts) == {"fit", "validation", "calibration"}
    return cohorts


def _observed_card_testing_dates(content: dict, config: SimulatorConfig) -> set[date]:
    timezone = ZoneInfo(config.timeline.business_timezone.value)
    attempts_per_card = config.fraud.card_testing.attempts_per_card.value
    window = timedelta(minutes=config.fraud.card_testing.window_minutes.value)
    low_amounts = config.fraud.card_testing
    rows_by_customer_date: dict[tuple[str, date], list[dict]] = {}
    for row in content["transactions"]:
        if row["channel"] != low_amounts.channel.value:
            continue
        business_date = _parsed_timestamp(row["created_at"]).astimezone(timezone).date()
        rows_by_customer_date.setdefault((row["customer_id"], business_date), []).append(row)

    observed_dates: set[date] = set()
    for (_, business_date), rows in rows_by_customer_date.items():
        ordered = sorted(rows, key=lambda row: row["created_at"])
        for index in range(len(ordered) - attempts_per_card):
            small = ordered[index : index + attempts_per_card]
            next_transaction = ordered[index + attempts_per_card]
            if (
                all(
                    low_amounts.minimum_attempt_amount.value
                    <= row["amount"]
                    <= low_amounts.maximum_attempt_amount.value
                    for row in small
                )
                and _parsed_timestamp(small[-1]["created_at"])
                - _parsed_timestamp(small[0]["created_at"])
                < window
                and _parsed_timestamp(next_transaction["created_at"])
                > _parsed_timestamp(small[-1]["created_at"])
                and next_transaction["amount"] > max(row["amount"] for row in small)
            ):
                observed_dates.add(business_date)
                break
    return observed_dates


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
    assert fraud_small_approvals == fraud_group["approved_small_attempt_count"]
    assert len(small_attempts) - fraud_small_approvals == fraud_group["declined_small_attempt_count"]
    followup_events = sorted(
        events_by_txn[followup["txn_id"]], key=lambda event: event["event_at"]
    )
    assert [event["event_type"] for event in followup_events] in (
        ["declined"],
        ["authorized", "settled"],
    )

    fraud_approved_total = 0
    fraud_declined_total = 0
    followup_approved_total = 0
    observed_lifecycle_count = 0
    observed_transaction_count = 0
    for group in (item for item in metadata["groups"] if item["role"] == "fraud"):
        group_approved = 0
        group_declined = 0
        group_transaction_ids = list(group["small_transaction_ids"])
        for txn_id in group["small_transaction_ids"]:
            event_types = [
                event["event_type"]
                for event in sorted(events_by_txn[txn_id], key=lambda event: event["event_at"])
            ]
            group_approved += event_types[0] == "authorized"
            group_declined += event_types[0] == "declined"
        assert group_approved == group["approved_small_attempt_count"]
        assert group_declined == group["declined_small_attempt_count"]
        fraud_approved_total += group_approved
        fraud_declined_total += group_declined
        followup_approved_total += group["larger_followup_approved"]
        group_transaction_ids.append(group["larger_followup_transaction_id"])
        observed_lifecycle_count += sum(
            len(events_by_txn[txn_id]) for txn_id in group_transaction_ids
        )
        observed_transaction_count += len(group_transaction_ids)
        assert sum(len(events_by_txn[txn_id]) for txn_id in group_transaction_ids) == (
            group["lifecycle_event_count"]
        )
    assert fraud_approved_total == summary.approved_small_attempt_count
    assert fraud_declined_total == summary.declined_small_attempt_count
    assert followup_approved_total == summary.approved_larger_followup_count
    assert summary.campaign_count == sum(
        group["role"] == "fraud" for group in metadata["groups"]
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
    control_authorized_count = sum(
        event["event_type"] == "authorized"
        for row in control_rows
        for event in events_by_txn[row["txn_id"]]
    )
    control_declined_count = sum(
        event["event_type"] == "declined"
        for row in control_rows
        for event in events_by_txn[row["txn_id"]]
    )
    assert control_authorized_count == control_group["approved_small_attempt_count"]
    assert control_declined_count == control_group["declined_small_attempt_count"]
    assert control_authorized_count == summary.approved_benign_control_count
    assert control_declined_count == summary.declined_benign_control_count
    control_lifecycle_count = sum(
        len(events_by_txn[row["txn_id"]]) for row in control_rows
    )
    assert control_lifecycle_count == control_group["lifecycle_event_count"]
    observed_lifecycle_count += control_lifecycle_count
    observed_transaction_count += len(control_rows)
    assert len(control_rows) == control_group["small_transaction_count"]
    assert summary.transaction_count == observed_transaction_count
    assert summary.lifecycle_event_count == observed_lifecycle_count
    assert generated["foreign_key_errors"] == []


def test_card_testing_supports_each_locked_p2_cohort(tmp_path: Path) -> None:
    config = load_simulator_config("configs/simulator.yaml")
    generated = _generate(
        tmp_path / "p2-cohort-support", config, include_fraud=True
    )
    observed_dates = _observed_card_testing_dates(generated["content"], config)
    model_cohorts = _model_cohorts_from_docs()

    for cohort_name, (cohort_start, cohort_end) in model_cohorts.items():
        assert any(cohort_start <= campaign_date < cohort_end for campaign_date in observed_dates), (
            f"no observable card-testing campaign in the locked {cohort_name} cohort "
            f"[{cohort_start}, {cohort_end})"
        )


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
