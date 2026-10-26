from __future__ import annotations

import hashlib
import json
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
from simulator.labels import label_delay_report
from simulator.reproducibility import (
    canonical_episode_content,
    canonical_episode_fingerprint,
)
from simulator.schema import PRIVATE_TABLES
from simulator.storage import create_export_stores, list_operational_schema


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
