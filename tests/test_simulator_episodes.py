from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

from simulator.asof import read_as_of
from simulator.config import (
    EpisodeConfig,
    load_episode_config,
    load_simulator_config,
    validate_episode_config,
)
from simulator.episodes import derive_episode_seed, generate_episodes
from simulator.incidents import inject_legit_business_spike, inject_new_fraud
from simulator.labels import label_delay_report
from simulator.reproducibility import (
    canonical_episode_content,
    canonical_episode_fingerprint,
)
from simulator.schema import PRIVATE_TABLES
from simulator.storage import create_export_stores, list_operational_schema
from simulator.transactions import LEGITIMATE_NEW_ACCOUNTS_METADATA_KEY
from simulator.world import ACCOUNT_PROFILE_METADATA_KEY


ROOT = Path(__file__).resolve().parents[1]
T0 = "2026-01-31T17:00:00.000000Z"


def _episode_config_with_assignments(
    assignments: list[dict[str, str]],
) -> EpisodeConfig:
    with (ROOT / "configs" / "episodes.yaml").open(encoding="utf-8") as stream:
        data = yaml.safe_load(stream)
    smoke = data["profiles"]["smoke"]
    smoke["assignments"] = assignments
    role_counts = Counter(item["role"] for item in assignments)
    clean_counts = Counter(
        item["role"] for item in assignments if item["template"] == "clean"
    )
    for role in ("development", "calibration", "test"):
        smoke["role_episode_counts"][role]["value"] = role_counts[role]
        smoke["clean_episode_counts"][role]["value"] = clean_counts[role]
    smoke["multi_cause_episode_count"]["value"] = 0
    return validate_episode_config(data)


def _generate(root: Path, config: EpisodeConfig | None = None):
    pair = create_export_stores(root)
    summary = generate_episodes(
        pair.operational,
        pair.private,
        load_simulator_config(ROOT / "configs" / "simulator.yaml"),
        config or load_episode_config(ROOT / "configs" / "episodes.yaml"),
    )
    pair.commit()
    return pair, summary


def _base_snapshot(connection: sqlite3.Connection) -> dict[str, list[dict]]:
    snapshot: dict[str, list[dict]] = {}
    for table in ("customers", "merchants", "transactions", "transaction_events", "label_events"):
        snapshot[table] = [
            dict(row)
            for row in connection.execute(
                f'SELECT * FROM "{table}" WHERE episode_id = \'base\' ORDER BY 1'
            )
        ]
    return snapshot


def _assert_operational_text_hides_episode_answers(connection: sqlite3.Connection) -> None:
    hidden_terms = (
        "data_pipeline",
        "seasonal",
        "clean",
        "development",
        "calibration",
        "test",
        "fraud_surge",
        "legit_business_spike",
        "legit_business_change",
        "new_fraud",
        "concept_drift",
    )
    schema = list_operational_schema(connection)
    for table, columns in schema.items():
        table_info = {
            row[1]: row[2].upper()
            for row in connection.execute(f'PRAGMA table_info("{table}")')
        }
        text_columns = {
            column
            for column in columns
            if table_info[column] == "TEXT" or column.lower().endswith("_id")
        }
        for row in connection.execute(f'SELECT * FROM "{table}"'):
            for column in text_columns:
                if table == "episodes" and column == "role":
                    assert row[column] in {"base", "development", "calibration", "test"}
                    continue
                value = row[column]
                if not isinstance(value, str):
                    continue
                lowered = value.lower()
                assert not any(term in lowered for term in hidden_terms), (
                    table,
                    column,
                    value,
                )


def test_smoke_manifest_is_private_locked_and_has_all_three_roles_and_templates(
    tmp_path: Path,
) -> None:
    pair, summary = _generate(tmp_path / "smoke")
    try:
        assignments = load_episode_config(ROOT / "configs" / "episodes.yaml").profile(
            "smoke"
        ).assignments
        expected_roles = {item.episode_id: item.role for item in assignments}
        expected_templates = {item.episode_id: item.template for item in assignments}
        manifest = {
            row["episode_id"]: dict(row)
            for row in pair.private.execute("SELECT * FROM episode_manifest")
        }
        operational_schema = list_operational_schema(pair.operational)
        account_metadata = json.loads(
            pair.private.execute(
                "SELECT value_json FROM run_metadata WHERE key = ?",
                (ACCOUNT_PROFILE_METADATA_KEY,),
            ).fetchone()[0]
        )
        legitimate_account_metadata = json.loads(
            pair.private.execute(
                "SELECT value_json FROM run_metadata WHERE key = ?",
                (LEGITIMATE_NEW_ACCOUNTS_METADATA_KEY,),
            ).fetchone()[0]
        )

        assert len(summary.episodes) == 5
        assert Counter(row["role"] for row in manifest.values()) == {
            "development": 1,
            "calibration": 2,
            "test": 2,
        }
        assert {row.episode_id for row in summary.episodes} == set(expected_roles)
        assert {row["template"] for row in manifest.values()} == {
            "clean",
            "data_pipeline",
            "seasonal",
        }
        assert {episode_id: row["role"] for episode_id, row in manifest.items()} == expected_roles
        assert {episode_id: row["template"] for episode_id, row in manifest.items()} == expected_templates
        assert set(operational_schema["episodes"]) == {"episode_id", "role"}
        assert "recipient_customer_id" in operational_schema["transactions"]
        assert not any(
            column in {"account_role", "role", "familiar_payees"}
            for table, columns in operational_schema.items()
            if table != "episodes"
            for column in columns
        )
        account_roles = account_metadata["account_roles"]
        assert set(account_roles.values()) == {
            "individual",
            "household_business",
            "internal_mule",
        }
        base_customer_ids = {
            row[0]
            for row in pair.operational.execute(
                "SELECT customer_id FROM customers WHERE episode_id = 'base'"
            )
        }
        assert set(account_roles) <= base_customer_ids
        assert pair.operational.execute(
            "SELECT 1 FROM customers WHERE episode_id <> 'base' LIMIT 1"
        ).fetchone()
        account_transfers = [
            dict(row)
            for row in pair.operational.execute(
                "SELECT * FROM transactions WHERE recipient_customer_id IS NOT NULL"
            )
        ]
        p14_transaction_ids: set[str] = set()
        p14_mule_ids: set[str] = set()
        for metadata_row in pair.private.execute(
            "SELECT value_json FROM run_metadata WHERE key LIKE 'p1.4.mechanisms.v1%'"
        ):
            groups = json.loads(metadata_row[0])["groups"]
            for group in groups:
                p14_transaction_ids.update(
                    txn_id
                    for field in (
                        "transaction_ids",
                        "incoming_transaction_ids",
                        "outgoing_transaction_ids",
                    )
                    for txn_id in group.get(field, [])
                )
                if isinstance(group.get("mule_customer_id"), str):
                    p14_mule_ids.add(group["mule_customer_id"])
        account_transfers = [
            row for row in account_transfers
            if row["customer_id"] in account_roles
            and row["recipient_customer_id"] in account_roles
            and row["txn_id"] not in p14_transaction_ids
        ]
        generated_transaction_ids = [
            row[0] for row in pair.operational.execute("SELECT txn_id FROM transactions")
        ]
        generated_customer_ids = [
            row[0] for row in pair.operational.execute("SELECT customer_id FROM customers")
        ]
        assert all(re.fullmatch(r"txn-[0-9a-f]{64}", item) for item in generated_transaction_ids)
        assert all(re.fullmatch(r"customer-[0-9]{6}", item) for item in generated_customer_ids)
        simulator_config = load_simulator_config(ROOT / "configs" / "simulator.yaml")
        legitimate_new_ids = set(legitimate_account_metadata["customer_ids"])
        assert legitimate_account_metadata["episode_id"] == "base"
        assert legitimate_account_metadata["count"] == (
            simulator_config.world.legitimate_new_accounts_per_base_history.value
        )
        assert len(legitimate_new_ids) == legitimate_account_metadata["count"]
        assert not legitimate_new_ids & p14_mule_ids
        history_start = pair.operational.execute(
            "SELECT MIN(event_at) FROM customers WHERE episode_id = 'base'"
        ).fetchone()[0]
        assert all(
            pair.operational.execute(
                "SELECT 1 FROM customers WHERE customer_id = ? AND episode_id = 'base' "
                "AND event_at > ?",
                (customer_id, history_start),
            ).fetchone()
            and pair.operational.execute(
                "SELECT 1 FROM transactions WHERE customer_id = ? AND episode_id = 'base' "
                "AND merchant_id IS NOT NULL AND recipient_customer_id IS NULL LIMIT 1",
                (customer_id,),
            ).fetchone()
            for customer_id in legitimate_new_ids
        )
        assert not pair.operational.execute(
            "SELECT 1 FROM transactions WHERE episode_id <> 'base' AND customer_id IN ("
            + ",".join("?" for _ in legitimate_new_ids)
            + ") LIMIT 1",
            tuple(sorted(legitimate_new_ids)),
        ).fetchone()
        household_accounts = {
            customer_id
            for customer_id, role in account_roles.items()
            if role == "household_business"
        }
        assert account_transfers
        assert household_accounts & {
            row["recipient_customer_id"] for row in account_transfers
        }
        assert all(
            account_roles[row["customer_id"]] == "individual"
            and account_roles[row["recipient_customer_id"]] != "internal_mule"
            and row["merchant_id"] is None
            and row["channel"] == "wallet"
            for row in account_transfers
        )
        supplier_merchants = account_metadata["supplier_merchants"]
        household_supplier_payments = [
            row
            for row in pair.operational.execute(
                "SELECT customer_id, merchant_id FROM transactions WHERE merchant_id IS NOT NULL"
            )
            if row["customer_id"] in account_roles
            and account_roles[row["customer_id"]] == "household_business"
            and row["merchant_id"] in supplier_merchants[row["customer_id"]]
        ]
        assert household_supplier_payments
        assert not set(PRIVATE_TABLES) & set(operational_schema)
        assert set(PRIVATE_TABLES) <= {
            row[0]
            for row in pair.private.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        _assert_operational_text_hides_episode_answers(pair.operational)

        episode_config = load_episode_config(ROOT / "configs" / "episodes.yaml")
        for episode_id, row in manifest.items():
            assert row["seed"] == derive_episode_seed(episode_config.seed.value, episode_id)
            assert len(row["content_fingerprint"]) == 64
            assert len(row["config_fingerprint"]) == 64
            config_json = json.loads(row["config_json"])
            canonical_config = json.dumps(
                config_json,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            assert hashlib.sha256(canonical_config.encode("utf-8")).hexdigest() == row[
                "config_fingerprint"
            ]
            assert config_json["episode_config"]["assignment"]["role"] == row["role"]
            assert config_json["episode_config"]["assignment"]["template"] == row["template"]
            assert row["content_fingerprint"] == canonical_episode_fingerprint(
                pair.operational, episode_id=episode_id, t0=T0
            )

        with pytest.raises(sqlite3.IntegrityError, match="manifest is locked"):
            pair.private.execute(
                "UPDATE episode_manifest SET role = 'test' WHERE episode_id = ?",
                ("ep-0001",),
            )
        with pytest.raises(sqlite3.IntegrityError, match="episode roles are locked"):
            pair.operational.execute(
                "UPDATE episodes SET role = 'test' WHERE episode_id = ?",
                ("ep-0001",),
            )
        assert pair.private.execute(
            "SELECT role FROM episode_manifest WHERE episode_id = 'ep-0001'"
        ).fetchone()[0] == "development"
    finally:
        pair.close()


def test_episode_data_isolated_asof_and_default_labels_calendar_do_not_leak(
    tmp_path: Path,
) -> None:
    pair, summary = _generate(tmp_path / "isolation")
    try:
        episode_ids = {episode.episode_id for episode in summary.episodes}
        simulator_config = load_simulator_config(ROOT / "configs" / "simulator.yaml")
        t0 = datetime.fromisoformat(T0.replace("Z", "+00:00"))
        episode_end = t0 + timedelta(days=30)
        base_snapshot = _base_snapshot(pair.operational)

        assert sum(1 for _ in summary.base_world.customer_ids) > 0
        assert summary.base_transaction_summary.transaction_count > 0
        assert len(base_snapshot["transactions"]) > summary.base_transaction_summary.transaction_count
        assert not pair.operational.execute(
            "SELECT 1 FROM business_calendar WHERE episode_id = 'base' LIMIT 1"
        ).fetchone()
        calendar_episode_ids = {
            row[0]
            for row in pair.operational.execute(
                "SELECT DISTINCT episode_id FROM business_calendar"
            )
        }
        assert calendar_episode_ids == {
            "ep-0003",
            "ep-0005",
        }

        for episode in summary.episodes:
            transactions = [
                dict(row)
                for row in pair.operational.execute(
                    "SELECT * FROM transactions WHERE episode_id = ? ORDER BY txn_id",
                    (episode.episode_id,),
                )
            ]
            assert transactions
            assert any(row["channel"] == "qr" for row in transactions)
            assert any(row["channel"] == "card" for row in transactions)
            for transaction in transactions:
                event_at = datetime.fromisoformat(
                    transaction["event_at"].replace("Z", "+00:00")
                )
                assert t0 <= event_at < episode_end
            assert episode.fraud_summary.transaction_count > 0

            snapshot = read_as_of(
                pair.operational,
                "transactions",
                episode_id=episode.episode_id,
                t0=T0,
                as_of_time="2026-03-10T00:00:00Z",
            )
            assert all(row["episode_id"] in {"base", episode.episode_id} for row in snapshot)
            assert all(row["available_at"] <= "2026-03-10T00:00:00.000000Z" for row in snapshot)

            label_snapshot = read_as_of(
                pair.operational,
                "label_events",
                episode_id=episode.episode_id,
                t0=T0,
                as_of_time="2026-03-10T00:00:00Z",
            )
            assert all(row["episode_id"] in {"base", episode.episode_id} for row in label_snapshot)
            delay_report = label_delay_report(
                pair.operational,
                pair.private,
                simulator_config,
                episode_id=episode.episode_id,
                as_of_time="2026-03-10T00:00:00Z",
            )
            assert delay_report["oracle"]["transaction_count"] == delay_report["observed"][
                "transaction_count"
            ]
            assert delay_report["oracle"]["true_fraud_count"] > 0
            label_metadata = json.loads(
                pair.private.execute(
                    "SELECT value_json FROM run_metadata WHERE key = ?",
                    (f"p1.5.label_truth.v1:{episode.episode_id}",),
                ).fetchone()[0]
            )
            assert label_metadata["episode_id"] == episode.episode_id
            assert all(
                truth_row["episode_id"] == episode.episode_id
                for truth_row in label_metadata["transactions"]
            )

            current_base = _base_snapshot(pair.operational)
            assert current_base == base_snapshot

        label_episode_ids = {
            row[0]
            for row in pair.operational.execute(
                "SELECT DISTINCT episode_id FROM label_events"
            )
        }
        assert label_episode_ids == {"base", *episode_ids}

        truth_counts = {
            row["episode_id"]: row["count"]
            for row in pair.private.execute(
                "SELECT episode_id, COUNT(*) AS count FROM incident_truth GROUP BY episode_id"
            )
        }
        assert truth_counts == {
            "ep-0002": 1,
            "ep-0003": 1,
            "ep-0004": 1,
            "ep-0005": 1,
        }
        assert all(
            pair.private.execute(
                "SELECT 1 FROM incident_truth WHERE episode_id = ?", (episode_id,)
            ).fetchone()
            is None
            for episode_id in episode_ids
            if episode_id == "ep-0001"
        )
    finally:
        pair.close()


def test_p16_truth_templates_reference_p14_groups_without_operational_leakage(
    tmp_path: Path,
) -> None:
    control, _ = _generate(tmp_path / "p16-template-control")
    treated, _ = _generate(tmp_path / "p16-template-treated")
    try:
        control_content = {
            table: [dict(row) for row in control.operational.execute(f'SELECT * FROM "{table}" ORDER BY 1')]
            for table in list_operational_schema(control.operational)
        }
        treated_before = {
            table: [dict(row) for row in treated.operational.execute(f'SELECT * FROM "{table}" ORDER BY 1')]
            for table in list_operational_schema(treated.operational)
        }
        assert control_content == treated_before

        social = inject_new_fraud(
            treated.operational,
            treated.private,
            load_simulator_config(ROOT / "configs" / "simulator.yaml"),
            episode_id="ep-0002",
            incident_truth_id="truth-p16-new-fraud",
        )
        business = inject_legit_business_spike(
            treated.operational,
            treated.private,
            load_simulator_config(ROOT / "configs" / "simulator.yaml"),
            episode_id="ep-0004",
            incident_truth_id="truth-p16-legit-business",
        )

        treated_after = {
            table: [dict(row) for row in treated.operational.execute(f'SELECT * FROM "{table}" ORDER BY 1')]
            for table in list_operational_schema(treated.operational)
        }
        assert treated_after == control_content
        social_truth = dict(
            treated.private.execute(
                "SELECT * FROM incident_truth WHERE incident_truth_id = ?",
                ("truth-p16-new-fraud",),
            ).fetchone()
        )
        business_truth = dict(
            treated.private.execute(
                "SELECT * FROM incident_truth WHERE incident_truth_id = ?",
                ("truth-p16-legit-business",),
            ).fetchone()
        )
        assert social_truth["cause"] == "new_fraud"
        assert social_truth["requires_intervention"] == 1
        assert social_truth["rule_covered"] == 0
        assert json.loads(social_truth["accepted_actions_json"]) == [
            "human_review",
            "fraud_ops",
        ]
        assert social_truth["effect_available_at"] == social.effect_available_at
        assert business_truth["cause"] == "legit_business_change"
        assert business_truth["requires_intervention"] == 0
        assert business_truth["rule_covered"] == 1
        assert json.loads(business_truth["accepted_actions_json"]) == []
        assert business_truth["effect_available_at"] == business.effect_available_at
        assert social.affected_transaction_ids and business.affected_transaction_ids
        assert "incident_truth" not in list_operational_schema(treated.operational)
        assert all(
            not {"cause", "mechanism", "accepted_actions_json"} & set(columns)
            for table, columns in list_operational_schema(treated.operational).items()
            if table != "decisions"
        )
    finally:
        control.close()
        treated.close()


def test_episode_add_remove_and_reorder_preserve_existing_data_and_roles(
    tmp_path: Path,
) -> None:
    base_config = load_episode_config(ROOT / "configs" / "episodes.yaml")
    assignments = [item.model_dump(mode="python") for item in base_config.profile("smoke").assignments]
    full_pair, _ = _generate(tmp_path / "full", base_config)
    reordered_pair, _ = _generate(
        tmp_path / "reordered", _episode_config_with_assignments(list(reversed(assignments)))
    )
    removed_id = "ep-0004"
    reduced_assignments = [item for item in assignments if item["episode_id"] != removed_id]
    reduced_pair, _ = _generate(
        tmp_path / "reduced", _episode_config_with_assignments(reduced_assignments)
    )
    added_assignments = assignments + [
        {"episode_id": "ep-0006", "role": "test", "template": "seasonal"}
    ]
    expanded_pair, _ = _generate(
        tmp_path / "expanded", _episode_config_with_assignments(added_assignments)
    )
    pairs = (full_pair, reordered_pair, reduced_pair, expanded_pair)
    try:
        snapshots = []
        for pair in pairs:
            snapshots.append(
                {
                    episode_id: (
                        canonical_episode_content(
                            pair.operational, episode_id=episode_id, t0=T0
                        ),
                        dict(
                            pair.private.execute(
                                "SELECT role, seed, template, content_fingerprint, config_fingerprint "
                                "FROM episode_manifest WHERE episode_id = ?",
                                (episode_id,),
                            ).fetchone()
                        ),
                    )
                    for episode_id in (
                        "ep-0001",
                        "ep-0002",
                        "ep-0003",
                        "ep-0005",
                    )
                }
            )
        for index in range(1, len(snapshots)):
            for episode_id in snapshots[0]:
                assert snapshots[index][episode_id] == snapshots[0][episode_id]

        base_snapshots = [_base_snapshot(pair.operational) for pair in pairs]
        assert all(snapshot == base_snapshots[0] for snapshot in base_snapshots[1:])
        assert full_pair.private.execute(
            "SELECT 1 FROM episode_manifest WHERE episode_id = ?", (removed_id,)
        ).fetchone()
        assert reduced_pair.private.execute(
            "SELECT 1 FROM episode_manifest WHERE episode_id = ?", (removed_id,)
        ).fetchone() is None
        assert expanded_pair.private.execute(
            "SELECT 1 FROM episode_manifest WHERE episode_id = 'ep-0006'"
        ).fetchone()
    finally:
        for pair in pairs:
            pair.close()
