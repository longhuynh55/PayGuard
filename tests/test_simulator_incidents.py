from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from evaluation.matcher import match_incidents
from simulator.config import load_episode_config, load_simulator_config
from simulator.incidents import (
    inject_data_pipeline,
    inject_fraud_surge_on_calendar_date,
    inject_sparse_new_merchant,
    inject_silent_concept_drift,
    inject_seasonal,
    inject_seasonal_unlisted,
    inject_two_cause_fraud_seasonal,
)
from simulator.labels import generate_label_events, label_truth_metadata_key
from simulator.storage import (
    create_export_stores,
    insert_operational_record,
    insert_private_record,
    list_operational_schema,
    utc_timestamp,
)
from simulator.validate import (
    _validate_incident_truth,
    _validate_p16_fraud_groups,
    _validate_parent_scenarios,
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


def _seed_empty_card_testing_truth(pair, episode_id: str) -> None:
    insert_private_record(
        pair.private,
        "run_metadata",
        {
            "key": f"p1.4.card_testing.v1:{episode_id}",
            "value_json": {
                "algorithm": "p1.4-card-testing-v2",
                "episode_id": episode_id,
                "mechanism": "card_testing",
                "groups": [],
            },
        },
    )


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
        assert "cause" in schema["decisions"]
        assert all(
            "cause" not in columns
            for table, columns in schema.items()
            if table != "decisions"
        )
        assert not {"mechanism", "accepted_actions_json"} & set(schema["decisions"])
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
        assert all(_timestamp(after_transactions[txn_id]["created_at"]).second != 0 for txn_id in added_ids)
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
            for table, columns in operational_schema.items()
            if table != "decisions"
        )
        assert "cause" in operational_schema["decisions"]
        assert not {"mechanism", "accepted_actions_json"} & set(operational_schema["decisions"])
        operational_text = json.dumps(after, sort_keys=True)
        for private_value in ("truth-seasonal", "incident_truth", "accepted_actions", "one_day_valid"):
            assert private_value not in operational_text
        assert "salary-day" in operational_text
    finally:
        control.close()
        treated.close()
        repeat.close()


def test_unlisted_seasonal_uses_same_seed_effect_without_calendar_or_rule_coverage(
    tmp_path: Path,
) -> None:
    config = load_simulator_config(ROOT / "configs" / "simulator.yaml")
    control = _make_episode_store(tmp_path / "unlisted-control")
    treated = _make_episode_store(tmp_path / "unlisted-treated")
    repeat = _make_episode_store(tmp_path / "unlisted-repeat")
    try:
        for pair in (control, treated, repeat):
            _seed_seasonal_background(pair)
        call_args = {
            "episode_id": "ep-incidents",
            "business_date": date(2026, 2, 6),
            "event_name": "unlisted-market-trend",
            "additional_transaction_count": 2,
            "expected_relative_change": 0.5,
            "seed": 731,
        }
        result = inject_seasonal_unlisted(
            treated.operational,
            treated.private,
            config,
            incident_truth_id="truth-unlisted-seasonal",
            **call_args,
        )
        repeat_result = inject_seasonal_unlisted(
            repeat.operational,
            repeat.private,
            config,
            incident_truth_id="truth-unlisted-seasonal",
            **call_args,
        )

        assert result == repeat_result
        assert _operational_content(treated) == _operational_content(repeat)
        assert result.calendar_event_id is None
        assert treated.operational.execute(
            "SELECT COUNT(*) FROM business_calendar WHERE episode_id = 'ep-incidents'"
        ).fetchone()[0] == 0
        control_ids = {
            row["txn_id"]
            for row in control.operational.execute("SELECT txn_id FROM transactions")
        }
        treated_ids = {
            row["txn_id"]
            for row in treated.operational.execute("SELECT txn_id FROM transactions")
        }
        assert treated_ids - control_ids == set(result.affected_transaction_ids)
        truth = _truth_row(treated, "truth-unlisted-seasonal")
        assert truth["cause"] == "seasonal"
        assert truth["rule_covered"] == 0
        assert truth["effect_available_at"] == min(
            row[0]
            for row in treated.operational.execute(
                "SELECT available_at FROM transactions WHERE txn_id IN (?, ?)",
                result.affected_transaction_ids,
            )
        )
    finally:
        control.close()
        treated.close()
        repeat.close()


def test_two_cause_fraud_seasonal_shares_parent_and_matcher_returns_both_truths(
    tmp_path: Path,
) -> None:
    config = load_simulator_config(ROOT / "configs" / "simulator.yaml")
    control = _make_episode_store(tmp_path / "two-cause-control", "ep-0003")
    treated = _make_episode_store(tmp_path / "two-cause-treated", "ep-0003")
    repeat = _make_episode_store(tmp_path / "two-cause-repeat", "ep-0003")
    try:
        _seed_empty_card_testing_truth(treated, "ep-0003")
        _seed_empty_card_testing_truth(repeat, "ep-0003")
        transaction_random_keys: dict[str, str] = {}
        repeat_random_keys: dict[str, str] = {}
        call_args = {
            "episode_id": "ep-0003",
            "fraud_truth_id": "truth-fraud-surge",
            "seasonal_truth_id": "truth-seasonal-component",
            "parent_scenario": "scenario-paired-1",
            "business_date": date(2026, 2, 6),
            "event_name": "scheduled-promotion",
            "seasonal_additional_transaction_count": 2,
            "seasonal_expected_relative_change": 0.5,
            "seed": 731,
            "transaction_random_keys": transaction_random_keys,
        }
        results = inject_two_cause_fraud_seasonal(
            treated.operational, treated.private, config, **call_args
        )
        repeated_call_args = {**call_args, "transaction_random_keys": repeat_random_keys}
        repeated_results = inject_two_cause_fraud_seasonal(
            repeat.operational, repeat.private, config, **repeated_call_args
        )
        generate_label_events(
            treated.operational,
            treated.private,
            config,
            episode_id="ep-0003",
            transaction_random_keys=transaction_random_keys,
        )
        generate_label_events(
            repeat.operational,
            repeat.private,
            config,
            episode_id="ep-0003",
            transaction_random_keys=repeat_random_keys,
        )
        assert results == repeated_results
        assert _operational_content(treated) == _operational_content(repeat)
        assert len(
            [row for row in _operational_content(control)["transactions"]]
        ) + sum(len(result.affected_transaction_ids) for result in results) == len(
            _operational_content(treated)["transactions"]
        )
        assert results[1].calendar_event_id is not None
        assert treated.operational.execute(
            "SELECT COUNT(*) FROM business_calendar WHERE episode_id = 'ep-0003'"
        ).fetchone()[0] == 1

        truths = [
            dict(row)
            for row in treated.private.execute(
                "SELECT * FROM incident_truth WHERE episode_id = 'ep-0003' "
                "ORDER BY incident_truth_id"
            )
        ]
        assert {truth["cause"] for truth in truths} == {"fraud_surge", "seasonal"}
        assert {truth["parent_scenario"] for truth in truths} == {"scenario-paired-1"}
        assert {truth["episode_id"] for truth in truths} == {"ep-0003"}
        truth_by_cause = {truth["cause"]: truth for truth in truths}
        assert max(
            _timestamp(truth_by_cause[cause]["effect_start_at"])
            for cause in ("fraud_surge", "seasonal")
        ) <= min(
            _timestamp(truth_by_cause[cause]["effect_end_at"])
            for cause in ("fraud_surge", "seasonal")
        )
        _validate_parent_scenarios({"ep-0003": truths})
        effect_manifests = {}
        for key, value_json in treated.private.execute(
            "SELECT key, value_json FROM run_metadata "
            "WHERE key LIKE 'p1.6.incident_effect.v1:%'"
        ):
            effect_manifests[key.rsplit(":", 1)[1]] = json.loads(value_json)
        _validate_p16_fraud_groups(
            treated.operational,
            treated.private,
            {"ep-0003": truths},
            effect_manifests,
        )
        assert all(
            json.loads(truth["accepted_actions_json"])
            == (["fraud_ops", "human_review"] if truth["cause"] == "fraud_surge" else [])
            for truth in truths
        )
        label_truth = json.loads(
            treated.private.execute(
                "SELECT value_json FROM run_metadata WHERE key = ?",
                (label_truth_metadata_key("ep-0003"),),
            ).fetchone()[0]
        )
        labels_by_id = {
            row["txn_id"]: row["true_label"] for row in label_truth["transactions"]
        }
        assert all(
            labels_by_id[txn_id] == expected_label
            for result, expected_label in zip(results, ("fraud", "legit"), strict=True)
            for txn_id in result.affected_transaction_ids
        )
        assert all(
            re.fullmatch(r"txn-[0-9a-f]{64}", txn_id)
            for result in results
            for txn_id in result.affected_transaction_ids
        )

        episode_config = load_episode_config(ROOT / "configs" / "episodes.yaml")
        insert_private_record(
            treated.private,
            "episode_manifest",
            {
                "episode_id": "ep-0003",
                "role": "calibration",
                "seed": 731,
                "template": "seasonal",
                "content_fingerprint": "0" * 64,
                "config_fingerprint": "0" * 64,
                "config_json": "{}",
            },
        )
        validator_metadata = {
            "resolved_config": {
                "simulator": config.model_dump(mode="json"),
                "episodes": episode_config.model_dump(mode="json"),
            }
        }
        assert _validate_incident_truth(
            treated.operational, treated.private, validator_metadata
        ) == "2 incident truth rows match locked templates and docs/data.md section 5"

        incident_at = max(truth["effect_available_at"] for truth in truths)
        match = match_incidents(
            [
                {
                    "incident_id": "incident-paired",
                    "episode_id": "ep-0003",
                    "feature": "txn_count",
                    "segment": "retail",
                    "opened_at": incident_at,
                    "alert_ids_json": '["alert-paired"]',
                    "event_at": incident_at,
                    "available_at": incident_at,
                }
            ],
            truths,
            episode_ids=["ep-0003"],
            episode_end_at={"ep-0003": "2026-03-02T17:00:00.000000Z"},
            as_of_time=utc_timestamp(_timestamp(incident_at) + timedelta(seconds=1)),
            metric_window_days=7,
        )
        assert match[0]["status"] == "matched"
        assert match[0]["matched_truth_ids"] == [
            "truth-fraud-surge",
            "truth-seasonal-component",
        ]
    finally:
        control.close()
        treated.close()
        repeat.close()


def test_parent_scenario_rejects_nonoverlapping_effect_windows() -> None:
    truths = [
        {
            "incident_truth_id": "truth-seasonal-nonoverlap",
            "cause": "seasonal",
            "parent_scenario": "parent-nonoverlap",
            "effect_start_at": "2026-02-06T00:49:11.000000Z",
            "effect_end_at": "2026-02-06T06:07:12.000000Z",
        },
        {
            "incident_truth_id": "truth-fraud-nonoverlap",
            "cause": "fraud_surge",
            "parent_scenario": "parent-nonoverlap",
            "effect_start_at": "2026-02-06T16:40:01.000000Z",
            "effect_end_at": "2026-02-06T16:51:02.000000Z",
        },
    ]
    with pytest.raises(ValueError, match="effect windows must overlap"):
        _validate_parent_scenarios({"ep-0003": truths})


def test_single_fraud_surge_can_land_on_a_public_calendar_date(
    tmp_path: Path,
) -> None:
    config = load_simulator_config(ROOT / "configs" / "simulator.yaml")
    control = _make_episode_store(tmp_path / "calendar-fraud-control")
    treated = _make_episode_store(tmp_path / "calendar-fraud-treated")
    repeat = _make_episode_store(tmp_path / "calendar-fraud-repeat")
    seasonal = _make_episode_store(tmp_path / "calendar-fraud-seasonal")
    try:
        seasonal_template = load_episode_config(
            ROOT / "configs" / "episodes.yaml"
        ).templates.seasonal
        call_args = {
            "episode_id": "ep-incidents",
            "incident_truth_id": "truth-calendar-fraud",
            "business_date": date(2026, 2, 8),
            "event_name": seasonal_template.event_name.value,
            "channel": config.transactions.channel.value,
            "expected_relative_change": seasonal_template.expected_relative_change.value,
            "seed": 902,
        }
        result = inject_fraud_surge_on_calendar_date(
            treated.operational, treated.private, config, **call_args
        )
        repeat_result = inject_fraud_surge_on_calendar_date(
            repeat.operational, repeat.private, config, **call_args
        )
        assert result == repeat_result
        assert _operational_content(treated) == _operational_content(repeat)
        inject_seasonal(
            seasonal.operational,
            seasonal.private,
            config,
            episode_id="ep-incidents",
            incident_truth_id="truth-seasonal-calendar-format",
            business_date=call_args["business_date"],
            event_name=seasonal_template.event_name.value,
            additional_transaction_count=seasonal_template.additional_transaction_count.value,
            expected_relative_change=seasonal_template.expected_relative_change.value,
            seed=903,
        )
        truth_rows = treated.private.execute(
            "SELECT cause, parent_scenario FROM incident_truth WHERE episode_id = 'ep-incidents'"
        ).fetchall()
        assert [tuple(row) for row in truth_rows] == [("fraud_surge", None)]
        calendar = treated.operational.execute(
            "SELECT calendar_event_id, business_date, event_name, lag_days, expected_effect_json, scope_json "
            "FROM business_calendar "
            "WHERE episode_id = 'ep-incidents'"
        ).fetchone()
        seasonal_calendar = seasonal.operational.execute(
            "SELECT business_date, event_name, lag_days, expected_effect_json, scope_json "
            "FROM business_calendar WHERE episode_id = 'ep-incidents'"
        ).fetchone()
        assert calendar[0] == result.calendar_event_id
        assert tuple(calendar[1:]) == tuple(seasonal_calendar)
        assert json.loads(calendar[4]) == {
            "txn_count": {
                "aggregation": "sum",
                "relative_change": seasonal_template.expected_relative_change.value,
            }
        }
        assert json.loads(calendar[5]) == {
            "channel": [config.transactions.channel.value]
        }
        assert all(
            _timestamp(row[0]).second != 0
            for row in treated.operational.execute(
                "SELECT created_at FROM transactions WHERE txn_id IN ("
                + ", ".join("?" for _ in result.affected_transaction_ids)
                + ")",
                result.affected_transaction_ids,
            )
        )
        control_transactions = control.operational.execute(
            "SELECT COUNT(*) FROM transactions"
        ).fetchone()[0]
        treated_transactions = treated.operational.execute(
            "SELECT COUNT(*) FROM transactions"
        ).fetchone()[0]
        assert treated_transactions - control_transactions == len(
            result.affected_transaction_ids
        )
    finally:
        control.close()
        treated.close()
        repeat.close()
        seasonal.close()


def test_sparse_new_merchant_truth_is_independent_of_sample_size(tmp_path: Path) -> None:
    config = load_simulator_config(ROOT / "configs" / "simulator.yaml")
    control = _make_episode_store(tmp_path / "sparse-merchant-control")
    fraud = _make_episode_store(tmp_path / "sparse-merchant-fraud")
    benign = _make_episode_store(tmp_path / "sparse-merchant-benign")
    try:
        for pair in (control, fraud, benign):
            _seed_empty_card_testing_truth(pair, "ep-incidents")
        call_args = {
            "episode_id": "ep-incidents",
            "incident_truth_id": "truth-sparse-merchant",
            "business_date": date(2026, 2, 8),
            "transaction_count": 3,
            "seed": 983,
        }
        fraud_result = inject_sparse_new_merchant(
            fraud.operational,
            fraud.private,
            config,
            truth_cause="new_fraud",
            **call_args,
        )
        benign_result = inject_sparse_new_merchant(
            benign.operational,
            benign.private,
            config,
            truth_cause="no_material_change",
            **call_args,
        )
        assert fraud_result == benign_result
        assert len(fraud_result.affected_transaction_ids) == 3
        assert fraud.operational.execute(
            "SELECT merchant_id, episode_id, mcc, province FROM merchants "
            "WHERE episode_id = 'ep-incidents'"
        ).fetchall() == benign.operational.execute(
            "SELECT merchant_id, episode_id, mcc, province FROM merchants "
            "WHERE episode_id = 'ep-incidents'"
        ).fetchall()
        merchant_event_at = fraud.operational.execute(
            "SELECT event_at FROM merchants WHERE episode_id = 'ep-incidents'"
        ).fetchone()[0]
        assert _timestamp(merchant_event_at).second != 0
        fraud_transactions = [
            tuple(row)
            for row in fraud.operational.execute(
                "SELECT txn_id, customer_id, merchant_id, created_at, event_at, "
                "available_at, amount, channel FROM transactions ORDER BY txn_id"
            )
        ]
        benign_transactions = [
            tuple(row)
            for row in benign.operational.execute(
                "SELECT txn_id, customer_id, merchant_id, created_at, event_at, "
                "available_at, amount, channel FROM transactions ORDER BY txn_id"
            )
        ]
        assert fraud_transactions == benign_transactions
        assert {
            row[0]
            for row in fraud.operational.execute(
                "SELECT DISTINCT episode_id FROM transactions "
                "WHERE txn_id IN (?, ?, ?)",
                fraud_result.affected_transaction_ids,
            )
        } == {"ep-incidents"}
        assert control.operational.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
        for pair, cause, actions in (
            (fraud, "new_fraud", ["human_review", "fraud_ops"]),
            (benign, "no_material_change", []),
        ):
            truth = _truth_row(pair, "truth-sparse-merchant")
            assert truth["cause"] == cause
            assert truth["mechanism"] == "new_merchant_sparse_history"
            assert truth["requires_intervention"] == int(cause == "new_fraud")
            assert truth["rule_covered"] == 0
            assert json.loads(truth["accepted_actions_json"]) == actions
            assert truth["effect_available_at"] == min(
                row[0]
                for row in pair.operational.execute(
                    "SELECT available_at FROM transactions WHERE txn_id IN (?, ?, ?)",
                    fraud_result.affected_transaction_ids,
                )
            )
            assert pair.operational.execute(
                "SELECT COUNT(*) FROM business_calendar WHERE episode_id = 'ep-incidents'"
            ).fetchone()[0] == 0
            assert all(
                re.fullmatch(r"txn-[0-9a-f]{64}", txn_id)
                for txn_id in fraud_result.affected_transaction_ids
            )
            assert all(
                _timestamp(row[3]).second != 0 for row in pair.operational.execute(
                    "SELECT txn_id, customer_id, merchant_id, created_at "
                    "FROM transactions WHERE txn_id IN (?, ?, ?)",
                    fraud_result.affected_transaction_ids,
                )
            )

        fraud_truths = [
            dict(row)
            for row in fraud.private.execute(
                "SELECT * FROM incident_truth WHERE episode_id = 'ep-incidents'"
            )
        ]
        fraud_effects = {
            key.rsplit(":", 1)[1]: json.loads(value_json)
            for key, value_json in fraud.private.execute(
                "SELECT key, value_json FROM run_metadata "
                "WHERE key LIKE 'p1.6.incident_effect.v1:%'"
            )
        }
        _validate_p16_fraud_groups(
            fraud.operational,
            fraud.private,
            {"ep-incidents": fraud_truths},
            fraud_effects,
        )

        generate_label_events(fraud.operational, fraud.private, config, episode_id="ep-incidents")
        generate_label_events(benign.operational, benign.private, config, episode_id="ep-incidents")
        for pair, expected_label in ((fraud, "fraud"), (benign, "legit")):
            label_truth = json.loads(
                pair.private.execute(
                    "SELECT value_json FROM run_metadata WHERE key = ?",
                    (label_truth_metadata_key("ep-incidents"),),
                ).fetchone()[0]
            )
            labels_by_id = {
                row["txn_id"]: row["true_label"] for row in label_truth["transactions"]
            }
            assert {
                labels_by_id[txn_id] for txn_id in fraud_result.affected_transaction_ids
            } == {expected_label}
    finally:
        control.close()
        fraud.close()
        benign.close()


def test_silent_concept_drift_uses_ordinary_rows_but_private_fraud_truth(
    tmp_path: Path,
) -> None:
    config = load_simulator_config(ROOT / "configs" / "simulator.yaml")
    control = _make_episode_store(tmp_path / "silent-drift-control")
    treated = _make_episode_store(tmp_path / "silent-drift-treated")
    repeat = _make_episode_store(tmp_path / "silent-drift-repeat")
    try:
        _seed_empty_card_testing_truth(treated, "ep-incidents")
        _seed_empty_card_testing_truth(repeat, "ep-incidents")
        random_keys: dict[str, str] = {}
        repeat_random_keys: dict[str, str] = {}
        args = {
            "episode_id": "ep-incidents",
            "incident_truth_id": "truth-silent-drift",
            "business_date": date(2026, 2, 10),
            "transaction_count": 3,
            "seed": 884,
        }
        result = inject_silent_concept_drift(
            treated.operational,
            treated.private,
            config,
            transaction_random_keys=random_keys,
            **args,
        )
        repeat_result = inject_silent_concept_drift(
            repeat.operational,
            repeat.private,
            config,
            transaction_random_keys=repeat_random_keys,
            **args,
        )
        generate_label_events(
            treated.operational,
            treated.private,
            config,
            episode_id="ep-incidents",
            transaction_random_keys=random_keys,
        )
        generate_label_events(
            repeat.operational,
            repeat.private,
            config,
            episode_id="ep-incidents",
            transaction_random_keys=repeat_random_keys,
        )
        assert result == repeat_result
        assert _operational_content(treated) == _operational_content(repeat)
        control_transactions = {
            row["txn_id"]: dict(row)
            for row in control.operational.execute("SELECT * FROM transactions")
        }
        treated_transactions = {
            row["txn_id"]: dict(row)
            for row in treated.operational.execute("SELECT * FROM transactions")
        }
        assert set(treated_transactions) - set(control_transactions) == set(
            result.affected_transaction_ids
        )
        assert all(
            config.transactions.minimum_amount.value
            <= treated_transactions[txn_id]["amount"]
            <= config.transactions.maximum_amount.value
            for txn_id in result.affected_transaction_ids
        )
        assert all(
            re.fullmatch(r"txn-[0-9a-f]{64}", txn_id)
            for txn_id in result.affected_transaction_ids
        )
        assert all(
            _timestamp(treated_transactions[txn_id]["created_at"]).second != 0
            for txn_id in result.affected_transaction_ids
        )
        truth = _truth_row(treated, "truth-silent-drift")
        assert truth["cause"] == "new_fraud"
        assert truth["mechanism"] == "silent_concept_drift"
        assert truth["rule_covered"] == 0
        assert json.loads(truth["accepted_actions_json"]) == ["human_review", "fraud_ops"]
        label_truth = json.loads(
            treated.private.execute(
                "SELECT value_json FROM run_metadata WHERE key = ?",
                (label_truth_metadata_key("ep-incidents"),),
            ).fetchone()[0]
        )
        assert {
            row["txn_id"]: row["true_label"] for row in label_truth["transactions"]
        } == {txn_id: "fraud" for txn_id in result.affected_transaction_ids}
        effect_manifest = json.loads(
            treated.private.execute(
                "SELECT value_json FROM run_metadata WHERE key = ?",
                (f"p1.6.incident_effect.v1:{truth['incident_truth_id']}",),
            ).fetchone()[0]
        )
        _validate_p16_fraud_groups(
            treated.operational,
            treated.private,
            {"ep-incidents": [truth]},
            {truth["incident_truth_id"]: effect_manifest},
        )
        assert truth["effect_available_at"] == min(
            treated_transactions[txn_id]["available_at"]
            for txn_id in result.affected_transaction_ids
        )
    finally:
        control.close()
        treated.close()
        repeat.close()
