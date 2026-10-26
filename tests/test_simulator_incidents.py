from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from simulator.config import load_simulator_config
from simulator.incidents import inject_data_pipeline, inject_seasonal
from simulator.storage import (
    create_export_stores,
    insert_operational_record,
    insert_private_record,
    list_operational_schema,
    utc_timestamp,
)


ROOT = Path(__file__).resolve().parents[1]


def _make_episode_store(root: Path, episode_id: str = "ep-incidents"):
    pair = create_export_stores(root)
    insert_operational_record(
        pair.operational, "episodes", {"episode_id": episode_id, "role": "development"}
    )
    insert_private_record(pair.private, "private_episodes", {"episode_id": episode_id})
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


def _add_transaction(
    pair,
    *,
    txn_id: str,
    episode_id: str,
    event_at: str,
    available_at: str | None = None,
    amount: int | None = 1000,
    channel: str = "qr",
) -> None:
    insert_operational_record(
        pair.operational,
        "transactions",
        {
            "txn_id": txn_id,
            "episode_id": episode_id,
            "customer_id": "customer-base",
            "merchant_id": "merchant-base",
            "created_at": event_at,
            "event_at": event_at,
            "available_at": available_at or event_at,
            "amount": amount,
            "channel": channel,
        },
    )


def _truth_row(pair, incident_truth_id: str) -> dict:
    row = pair.private.execute(
        "SELECT * FROM incident_truth WHERE incident_truth_id = ?", (incident_truth_id,)
    ).fetchone()
    assert row is not None
    return dict(row)


def _json_column(truth: dict, column: str):
    return json.loads(truth[f"{column}_json"])


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _operational_content(pair) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    for table in sorted(list_operational_schema(pair.operational)):
        result[table] = [
            dict(row)
            for row in pair.operational.execute(f'SELECT * FROM "{table}" ORDER BY 1')
        ]
    return result


@pytest.mark.parametrize(
    ("mode", "expected_amounts", "expected_mechanism"),
    [
        ("multiply_100", {"txn-pipeline-a": 125000, "txn-pipeline-b": 250000}, "amount_multiplier_100"),
        ("null", {"txn-pipeline-a": None, "txn-pipeline-b": None}, "amount_null_burst"),
    ],
)
def test_data_pipeline_changes_only_post_start_episode_amounts_and_keeps_truth_private(
    tmp_path: Path, mode: str, expected_amounts: dict[str, int | None], expected_mechanism: str
) -> None:
    config = load_simulator_config(ROOT / "configs" / "simulator.yaml")
    control = _make_episode_store(tmp_path / f"control-{mode}")
    treated = _make_episode_store(tmp_path / f"treated-{mode}")
    try:
        rows = [
            ("txn-base-history", "base", "2026-01-30T10:00:00Z", 50),
            ("txn-before-pipeline", "ep-incidents", "2026-01-31T16:59:00Z", 75),
            ("txn-pipeline-a", "ep-incidents", "2026-02-01T18:00:00Z", 1250),
            ("txn-pipeline-b", "ep-incidents", "2026-02-02T00:00:00Z", 2500),
            ("txn-already-null", "ep-incidents", "2026-02-02T01:00:00Z", None),
        ]
        for target in (control, treated):
            for txn_id, episode_id, event_at, amount in rows:
                available = event_at
                if txn_id == "txn-pipeline-a":
                    available = "2026-02-01T22:00:00Z"
                elif txn_id == "txn-pipeline-b":
                    available = "2026-02-02T04:00:00Z"
                _add_transaction(
                    target,
                    txn_id=txn_id,
                    episode_id=episode_id,
                    event_at=event_at,
                    available_at=available,
                    amount=amount,
                )

        result = inject_data_pipeline(
            treated.operational,
            treated.private,
            config,
            episode_id="ep-incidents",
            incident_truth_id=f"truth-pipeline-{mode}",
            mode=mode,  # type: ignore[arg-type]
        )

        control_rows = {
            row["txn_id"]: dict(row)
            for row in control.operational.execute("SELECT * FROM transactions")
        }
        treated_rows = {
            row["txn_id"]: dict(row)
            for row in treated.operational.execute("SELECT * FROM transactions")
        }
        assert set(control_rows) == set(treated_rows)
        for txn_id in control_rows:
            expected = dict(control_rows[txn_id])
            if txn_id in expected_amounts:
                expected["amount"] = expected_amounts[txn_id]
            assert treated_rows[txn_id] == expected
        assert result.affected_transaction_ids == ("txn-pipeline-a", "txn-pipeline-b")
        assert result.effect_available_at == "2026-02-01T22:00:00.000000Z"

        truth = _truth_row(treated, f"truth-pipeline-{mode}")
        assert set(truth) == {
            "incident_truth_id", "episode_id", "cause", "requires_intervention", "mechanism",
            "rule_covered", "affected_entities_json", "affected_segments_json",
            "affected_features_json", "effect_start_at", "effect_end_at",
            "effect_available_at", "intervention_start_at", "deadline_at",
            "accepted_actions_json", "parent_scenario",
        }
        truth_types = {
            row[1]: row[2].upper()
            for row in treated.private.execute("PRAGMA table_info(incident_truth)")
        }
        assert truth_types == {
            "incident_truth_id": "TEXT",
            "episode_id": "TEXT",
            "cause": "TEXT",
            "requires_intervention": "INTEGER",
            "mechanism": "TEXT",
            "rule_covered": "INTEGER",
            "affected_entities_json": "TEXT",
            "affected_segments_json": "TEXT",
            "affected_features_json": "TEXT",
            "effect_start_at": "TEXT",
            "effect_end_at": "TEXT",
            "effect_available_at": "TEXT",
            "intervention_start_at": "TEXT",
            "deadline_at": "TEXT",
            "accepted_actions_json": "TEXT",
            "parent_scenario": "TEXT",
        }
        assert "incident_truth" in {
            row[0]
            for row in treated.private.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert truth["cause"] == "data_pipeline"
        assert truth["requires_intervention"] == 1
        assert truth["mechanism"] == expected_mechanism
        assert truth["rule_covered"] == 1
        assert _json_column(truth, "affected_entities") == ["customer-base", "merchant-base"]
        assert _json_column(truth, "affected_segments") == ["retail"]
        assert _json_column(truth, "affected_features") == ["amount"]
        assert _json_column(truth, "accepted_actions") == ["data_ticket", "human_review"]
        assert truth["parent_scenario"] is None
        assert truth["effect_available_at"] == result.effect_available_at
        assert truth["intervention_start_at"] == result.effect_available_at
        assert truth["deadline_at"] == utc_timestamp(
            _timestamp(result.effect_available_at) + timedelta(days=7)
        )

        operational_content = json.dumps(_operational_content(treated), sort_keys=True)
        schema = list_operational_schema(treated.operational)
        assert "incident_truth" not in schema
        assert all("cause" not in columns for columns in schema.values())
        assert "truth-pipeline" not in operational_content
        assert "data_pipeline" not in operational_content
        assert "accepted_actions" not in operational_content
    finally:
        control.close()
        treated.close()


def _seed_seasonal_background(pair) -> None:
    local = ZoneInfo("Asia/Ho_Chi_Minh")
    history = datetime(2026, 1, 30, 9, tzinfo=local).astimezone(ZoneInfo("UTC"))
    _add_transaction(
        pair,
        txn_id="txn-base-history",
        episode_id="base",
        event_at=utc_timestamp(history),
        amount=25000,
    )
    day_counts = {date(2026, 2, 5): 1, date(2026, 2, 6): 4, date(2026, 2, 7): 2}
    for business_date, count in day_counts.items():
        midnight = datetime.combine(business_date, datetime.min.time(), tzinfo=local)
        for index in range(count):
            created = midnight + timedelta(hours=9, minutes=index * 3)
            _add_transaction(
                pair,
                txn_id=f"txn-bg-{business_date:%Y%m%d}-{index}",
                episode_id="ep-incidents",
                event_at=utc_timestamp(created),
                amount=10000 + index,
            )


def test_seasonal_is_one_day_and_calendar_publishes_expectation_without_truth(
    tmp_path: Path,
) -> None:
    config = load_simulator_config(ROOT / "configs" / "simulator.yaml")
    control = _make_episode_store(tmp_path / "seasonal-control")
    treated = _make_episode_store(tmp_path / "seasonal-treated")
    repeat = _make_episode_store(tmp_path / "seasonal-repeat")
    try:
        _seed_seasonal_background(control)
        _seed_seasonal_background(treated)
        _seed_seasonal_background(repeat)
        control_content = _operational_content(control)
        before = _operational_content(treated)
        assert before == control_content

        call_args = {
            "episode_id": "ep-incidents",
            "business_date": date(2026, 2, 6),
            "event_name": "salary-day",
            "additional_transaction_count": 2,
            "expected_relative_change": 0.5,
            "seed": 731,
        }
        result = inject_seasonal(
            treated.operational,
            treated.private,
            config,
            incident_truth_id="truth-seasonal",
            **call_args,
        )
        repeated_result = inject_seasonal(
            repeat.operational,
            repeat.private,
            config,
            incident_truth_id="truth-seasonal",
            **call_args,
        )

        after = _operational_content(treated)
        assert after == _operational_content(repeat)
        assert result == repeated_result
        before_transactions = {row["txn_id"]: row for row in before["transactions"]}
        after_transactions = {row["txn_id"]: row for row in after["transactions"]}
        added_ids = set(after_transactions) - set(before_transactions)
        assert added_ids == set(result.affected_transaction_ids)
        assert len(added_ids) == 2
        assert all(after_transactions[txn_id]["episode_id"] == "ep-incidents" for txn_id in added_ids)
        assert all(after_transactions[txn_id]["amount"] > 0 for txn_id in added_ids)
        assert all(after_transactions[txn_id]["channel"] == "qr" for txn_id in added_ids)
        for txn_id, row in before_transactions.items():
            assert after_transactions[txn_id] == row
        control_transactions = {
            row["txn_id"]: row for row in control_content["transactions"]
        }
        assert all(after_transactions[txn_id] == row for txn_id, row in control_transactions.items())
        assert all(
            after[table] == control_content[table]
            for table in ("customers", "episodes", "label_events", "merchants")
        )
        assert [
            row for row in after["transaction_events"] if row["txn_id"] not in added_ids
        ] == control_content["transaction_events"]
        assert [row for row in after["business_calendar"] if row["episode_id"] == "ep-incidents"] == [
            {
                "calendar_event_id": result.calendar_event_id,
                "episode_id": "ep-incidents",
                "event_at": "2026-02-05T17:00:00.000000Z",
                "available_at": "2026-02-05T17:00:00.000000Z",
                "business_date": "2026-02-06",
                "event_name": "salary-day",
                "lag_days": 0,
                "expected_effect_json": '{"txn_count":{"aggregation":"sum","relative_change":0.5}}',
                "scope_json": '{"channel":["qr"]}',
            }
        ]
        assert [row for row in control_content["business_calendar"] if row["episode_id"] == "ep-incidents"] == []

        local = ZoneInfo("Asia/Ho_Chi_Minh")
        counts_by_date: dict[date, int] = {}
        for row in after["transactions"]:
            if row["episode_id"] != "ep-incidents":
                continue
            local_day = _timestamp(row["event_at"]).astimezone(local).date()
            counts_by_date[local_day] = counts_by_date.get(local_day, 0) + 1
        assert counts_by_date[date(2026, 2, 6)] == 6
        assert counts_by_date[date(2026, 2, 5)] == 1
        assert counts_by_date[date(2026, 2, 7)] == 2

        new_events = [
            row for row in after["transaction_events"]
            if row["txn_id"] in added_ids
        ]
        assert len(new_events) == 4
        assert {row["event_type"] for row in new_events} == {"authorized", "settled"}
        assert all(row["episode_id"] == "ep-incidents" for row in new_events)
        assert all(row["available_at"] >= row["event_at"] for row in new_events)

        calendar = treated.operational.execute(
            "SELECT * FROM business_calendar WHERE calendar_event_id = ?",
            (result.calendar_event_id,),
        ).fetchone()
        assert calendar is not None
        calendar = dict(calendar)
        assert calendar["episode_id"] == "ep-incidents"
        assert calendar["business_date"] == "2026-02-06"
        assert calendar["event_name"] == "salary-day"
        assert calendar["lag_days"] == 0
        assert json.loads(calendar["expected_effect_json"]) == {
            "txn_count": {"aggregation": "sum", "relative_change": 0.5}
        }
        assert json.loads(calendar["scope_json"]) == {"channel": ["qr"]}
        assert calendar["event_at"] == "2026-02-05T17:00:00.000000Z"
        assert calendar["available_at"] == calendar["event_at"]

        truth = _truth_row(treated, "truth-seasonal")
        assert truth["cause"] == "seasonal"
        assert truth["requires_intervention"] == 0
        assert truth["rule_covered"] == 1
        assert _json_column(truth, "accepted_actions") == []
        assert _json_column(truth, "affected_features") == ["txn_count"]
        affected_rows = [after_transactions[txn_id] for txn_id in sorted(added_ids)]
        assert truth["effect_available_at"] == min(row["available_at"] for row in affected_rows)
        assert truth["effect_start_at"] == min(row["event_at"] for row in affected_rows)
        assert truth["effect_end_at"] == max(row["event_at"] for row in affected_rows)
        assert truth["intervention_start_at"] == truth["effect_available_at"]
        assert truth["deadline_at"] == utc_timestamp(
            _timestamp(truth["effect_available_at"]) + timedelta(days=7)
        )

        # Calendar expectations are public; cause, mechanism, actions, and truth stay private.
        operational_schema = list_operational_schema(treated.operational)
        assert "incident_truth" not in operational_schema
        assert all(
            not {"cause", "mechanism", "accepted_actions_json"} & set(columns)
            for columns in operational_schema.values()
        )
        operational_text = json.dumps(after, sort_keys=True)
        for private_value in ("truth-seasonal", "incident_truth", "accepted_actions", "one_day_valid"):
            assert private_value not in operational_text
        assert "salary-day" in operational_text
    finally:
        control.close()
        treated.close()
        repeat.close()
