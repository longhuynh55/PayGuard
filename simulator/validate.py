"""Validate P1 smoke run contracts without suppressing any violations."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from simulator.asof import read_as_of
from simulator.config import (
    validate_episode_config,
    validate_episode_duration_match,
    validate_simulator_config,
)
from simulator.episodes import derive_episode_seed
from simulator.labels import LABEL_POLICY_VERSION, snapshot_labels
from simulator.randomness import keyed_rng
from simulator.reproducibility import (
    RUN_FINGERPRINT_METADATA_KEY,
    canonical_episode_fingerprint,
    canonical_run_fingerprint,
)
from simulator.schema import (
    AS_OF_TABLES,
    OPERATIONAL_DDL,
    OPERATIONAL_TABLES,
    OPERATIONAL_TIMESTAMP_COLUMNS,
    PRIVATE_DDL,
    PRIVATE_TABLES,
    T0_UTC_TEXT,
)
from simulator.storage import StorageError, utc_timestamp
from triage.decisions import normalize_decision_real_columns


RUN_METADATA_KEY = "p1.8.run_metadata.v1"
VALIDATION_METADATA_KEY = "p1.8.validation.v1"
SCHEMA_VERSION = 1
# Contract value in docs/data.md, section 5 (MVP D = 7 days).
MVP_INCIDENT_DEADLINE_DAYS = 7
_RESERVED_ID_TOKENS = {
    "truth",
    "fraud",
    "seasonal",
    "pipeline",
    "inject",
    "clean",
    "cause",
    "mechanism",
    "oracle",
    "development",
    "calibration",
    "test",
}
_PUBLIC_FORBIDDEN_KEYS = {
    "oracle",
    "truth",
    "truth_bearing",
    "truth_label_support",
    "label_delay_oracle",
    "incident_truth_counts_by_cause",
    "cause",
    "mechanism",
    "template",
    "true_label",
    "true_fraud_count",
    "truth_positive_count",
    "incident_truth_id",
    "pre_injection_amount_by_txn_id",
    "accepted_actions",
    "effect_available_at",
    "parent_scenario",
    "requires_intervention",
    "rule_covered",
}
_DOWNSTREAM_OUTPUT_TABLES = (
    "model_scores",
    "daily_metrics",
    "alerts",
    "incidents",
    "decisions",
)
_MONITORING_PRIVATE_MARKERS = _RESERVED_ID_TOKENS
_MONITORING_PRIVATE_FIELD_MARKERS = _PUBLIC_FORBIDDEN_KEYS | {
    "accepted_actions_json",
    "affected_entities_json",
    "affected_features_json",
    "affected_segments_json",
    "config_fingerprint",
    "config_json",
    "content_fingerprint",
    "deadline_at",
    "effect_end_at",
    "effect_start_at",
    "incident_effect",
    "incident_truth",
    "intervention_start_at",
    "p1.6.incident_effect.v1",
    "seed",
    "template",
    "txn_ids",
}
_MONITORING_PRIVATE_FIELD_PATTERNS = tuple(
    re.compile(rf"(?<![a-z0-9]){re.escape(marker)}(?![a-z0-9])")
    for marker in sorted(_MONITORING_PRIVATE_FIELD_MARKERS, key=len, reverse=True)
)
_MONITORING_QUALITY_FLAGS = frozenset(
    {
        "window_incomplete",
        "small_window_sample",
        "small_reference_sample",
        "no_window_samples",
        "no_reference_samples",
        "calendar_expectation_unavailable",
    }
)
_MONITORING_COUNT_FLAG = re.compile(
    r"^(?:window_censored_samples|reference_censored_samples|"
    r"window_missing_samples|reference_missing_samples|"
    r"target_unavailable_at_decision|reference_targets_unavailable_at_decision)=[1-9][0-9]*$"
)


class ValidationError(ValueError):
    """Raised internally for a failed contract check."""


def _fail(message: str) -> None:
    raise ValidationError(message)


def _canonical_json_hash(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _open_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _open_private(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _normalized_sql(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


def _schema_objects(connection: sqlite3.Connection) -> dict[tuple[str, str], str]:
    rows = connection.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' AND type IN ('table', 'trigger', 'view', 'index') "
        "ORDER BY type, name"
    ).fetchall()
    return {
        (row["type"], row["name"]): _normalized_sql(row["sql"])
        for row in rows
        if row["sql"] is not None
    }


def _expected_schema(ddl: tuple[str, ...], *, operational: bool) -> dict[tuple[str, str], str]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        for statement in ddl:
            connection.execute(statement)
        if operational:
            connection.execute(
                "INSERT INTO episodes (episode_id, role) VALUES ('base', 'base')"
            )
        return _schema_objects(connection)
    finally:
        connection.close()


def _check_schema(connection: sqlite3.Connection, *, operational: bool) -> str:
    actual = _schema_objects(connection)
    expected = _expected_schema(
        OPERATIONAL_DDL if operational else PRIVATE_DDL,
        operational=operational,
    )
    actual_tables = {name for (kind, name) in actual if kind == "table"}
    expected_tables = set(OPERATIONAL_TABLES if operational else PRIVATE_TABLES)
    if actual_tables != expected_tables:
        _fail(
            f"table set differs (missing={sorted(expected_tables - actual_tables)}, "
            f"unexpected={sorted(actual_tables - expected_tables)})"
        )
    if actual != expected:
        differences = []
        for key in sorted(set(actual) | set(expected)):
            if actual.get(key) != expected.get(key):
                differences.append(f"{key[0]} {key[1]}")
        _fail("schema objects differ from the contract: " + ", ".join(differences[:12]))

    for table in expected_tables:
        columns = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        primary_keys = [column for column in columns if column["pk"]]
        if not primary_keys or any(not column["notnull"] for column in primary_keys):
            _fail(f"{table} must have explicit NOT NULL primary-key columns")
    return f"{len(expected_tables)} tables and their keys, checks, triggers, and columns match"


def _integrity_check(connection: sqlite3.Connection, label: str) -> str:
    integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
    if integrity != ["ok"]:
        _fail(f"{label} integrity_check failed: {integrity[:5]}")
    foreign_keys = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")]
    if foreign_keys:
        _fail(f"{label} foreign_key_check found violations: {foreign_keys[:5]}")
    return f"{label} integrity_check=ok; foreign_key_check=0"


def _load_run_metadata(private: sqlite3.Connection) -> dict[str, Any]:
    row = private.execute(
        "SELECT value_json FROM run_metadata WHERE key = ?", (RUN_METADATA_KEY,)
    ).fetchone()
    if row is None:
        _fail(f"private run metadata {RUN_METADATA_KEY!r} is missing")
    try:
        metadata = json.loads(row[0])
    except (TypeError, json.JSONDecodeError) as exc:
        _fail(f"private run metadata is invalid JSON: {exc}")
    if not isinstance(metadata, dict):
        _fail("private run metadata must be a JSON object")
    return metadata


def _validate_metadata(
    operational: sqlite3.Connection,
    private: sqlite3.Connection,
    metadata: dict[str, Any],
) -> str:
    expected = {
        "artifact_version",
        "profile",
        "resolved_config",
        "seed",
        "versions",
        "config_fingerprint",
        "canonical_content_fingerprint",
        "runtime",
        "validation",
    }
    missing = sorted(expected - set(metadata))
    if missing:
        _fail(f"run metadata is missing fields: {missing}")
    if metadata["artifact_version"] != SCHEMA_VERSION or metadata["profile"] != "smoke":
        _fail("run metadata artifact version/profile is not P1.8 smoke v1")
    versions = metadata["versions"]
    if not isinstance(versions, dict) or versions.get("schema") != SCHEMA_VERSION:
        _fail("run metadata schema version is missing or unsupported")
    if not isinstance(versions.get("code"), str) or not versions["code"]:
        _fail("run metadata code version must be recorded")
    if versions.get("git_commit") is not None and not re.fullmatch(
        r"[0-9a-f]{40,64}", str(versions["git_commit"])
    ):
        _fail("run metadata git commit must be a full hexadecimal object ID or null")
    resolved = metadata["resolved_config"]
    if not isinstance(resolved, dict) or not {
        "simulator",
        "episodes",
        "model_support",
    } <= set(resolved):
        _fail("run metadata must retain resolved simulator, episode, and P2 support configs")
    if _canonical_json_hash(resolved) != metadata["config_fingerprint"]:
        _fail("resolved configuration fingerprint does not match its metadata")
    for name in ("simulator", "episodes"):
        if not isinstance(resolved[name], dict):
            _fail(f"resolved {name} config must be a JSON object")
    simulator_config = validate_simulator_config(resolved["simulator"])
    episode_config = validate_episode_config(resolved["episodes"])
    validate_episode_duration_match(simulator_config, episode_config)
    if simulator_config.seed.value != metadata["seed"].get("simulator"):
        _fail("recorded simulator seed differs from resolved config")
    if episode_config.seed.value != metadata["seed"].get("episodes"):
        _fail("recorded episode seed differs from resolved config")

    stored_fingerprint = metadata.get("canonical_content_fingerprint")
    computed_fingerprint = canonical_run_fingerprint(
        operational,
        empty_tables=_DOWNSTREAM_OUTPUT_TABLES,
    )
    if stored_fingerprint != computed_fingerprint:
        _fail(
            "canonical operational fingerprint differs "
            f"(stored={stored_fingerprint}, computed={computed_fingerprint})"
        )
    fp_row = private.execute(
        "SELECT value_json FROM run_metadata WHERE key = ?",
        (RUN_FINGERPRINT_METADATA_KEY,),
    ).fetchone()
    if fp_row is None:
        _fail(f"private canonical fingerprint {RUN_FINGERPRINT_METADATA_KEY!r} is missing")
    fp_metadata = json.loads(fp_row[0])
    if fp_metadata.get("fingerprint") != computed_fingerprint:
        _fail("private canonical fingerprint record does not match operational data")

    runtime = metadata["runtime"]
    if not isinstance(runtime, dict) or not {
        "started_at",
        "completed_at",
        "elapsed_seconds",
    } <= set(runtime):
        _fail("run metadata must include start, completion, and elapsed runtime")
    for field in ("started_at", "completed_at"):
        if utc_timestamp(runtime[field]) != runtime[field]:
            _fail(f"run metadata {field} must be fixed-width UTC")
    if not isinstance(runtime["elapsed_seconds"], (int, float)) or runtime["elapsed_seconds"] < 0:
        _fail("run metadata elapsed_seconds must be non-negative")
    return (
        "resolved configs and seeds validate; config/code/schema versions, runtime, "
        "and canonical fingerprint are recorded in private metadata"
    )


def _validate_episode_manifest(
    operational: sqlite3.Connection,
    private: sqlite3.Connection,
    metadata: dict[str, Any],
) -> str:
    operational_rows = {
        row["episode_id"]: row["role"]
        for row in operational.execute("SELECT episode_id, role FROM episodes")
    }
    if operational_rows.get("base") != "base":
        _fail("operational episodes must contain exactly one base sentinel")
    if sum(role == "base" for role in operational_rows.values()) != 1:
        _fail("operational episodes must have exactly one base role")
    non_base = {key: value for key, value in operational_rows.items() if key != "base"}
    private_episode_ids = {
        row[0] for row in private.execute("SELECT episode_id FROM private_episodes")
    }
    manifest = {
        row["episode_id"]: dict(row)
        for row in private.execute("SELECT * FROM episode_manifest")
    }
    if set(non_base) != private_episode_ids or set(non_base) != set(manifest):
        _fail("operational episodes, private episode registry, and manifest IDs differ")
    episode_config = validate_episode_config(metadata["resolved_config"]["episodes"])
    assignments = {
        item.episode_id: item
        for item in episode_config.profile("smoke").assignments
    }
    expected_roles = {episode_id: item.role for episode_id, item in assignments.items()}
    if non_base != expected_roles:
        _fail("operational episode roles differ from the locked private smoke assignments")
    for episode_id, role in non_base.items():
        entry = manifest[episode_id]
        assignment = assignments[episode_id]
        if entry["role"] != role:
            _fail(f"episode role differs between operational and private manifest: {episode_id}")
        if entry["template"] != assignment.template:
            _fail(f"episode template differs from its locked private assignment: {episode_id}")
        if entry["seed"] != derive_episode_seed(episode_config.seed.value, episode_id):
            _fail(f"episode seed differs from its root seed and ID: {episode_id}")
        config_text = entry["config_json"]
        if _canonical_json_hash(json.loads(config_text)) != entry["config_fingerprint"]:
            _fail(f"episode config fingerprint mismatch: {episode_id}")
        fingerprint = canonical_episode_fingerprint(
            operational, episode_id=episode_id, t0=T0_UTC_TEXT
        )
        if fingerprint != entry["content_fingerprint"]:
            _fail(f"episode content fingerprint mismatch: {episode_id}")
    return f"{len(non_base)} locked smoke episode roles and private manifest entries match"


def _validate_transaction_event_episode_window(
    operational: sqlite3.Connection,
    metadata: dict[str, Any],
) -> str:
    """Require every non-base transaction event to remain inside the episode future."""
    simulator_config = validate_simulator_config(metadata["resolved_config"]["simulator"])
    episode_config = validate_episode_config(metadata["resolved_config"]["episodes"])
    timezone = ZoneInfo(simulator_config.timeline.business_timezone.value)
    episode_start = datetime.combine(
        simulator_config.timeline.t0_business_date.value,
        time.min,
        tzinfo=timezone,
    ).astimezone(UTC)
    episode_end = episode_start + timedelta(days=episode_config.protocol.duration_days.value)
    event_count = 0
    for event in operational.execute(
        "SELECT event_id, episode_id, event_at, available_at FROM transaction_events "
        "WHERE episode_id <> 'base' ORDER BY episode_id, event_at, event_id"
    ):
        for field in ("event_at", "available_at"):
            try:
                value = datetime.fromisoformat(
                    event[field].replace("Z", "+00:00")
                ).astimezone(UTC)
            except (AttributeError, TypeError, ValueError) as exc:
                _fail(
                    f"transaction event {event['event_id']} has invalid {field} "
                    f"for {event['episode_id']}: {exc}"
                )
            if not episode_start <= value < episode_end:
                _fail(
                    f"transaction event {event['event_id']} {field} is outside the "
                    f"episode future window for {event['episode_id']}"
                )
        event_count += 1
    return (
        f"{event_count} non-base transaction events have event_at and available_at "
        "inside [T0, T0 + duration_days)"
    )


def _validate_incident_truth(
    operational: sqlite3.Connection,
    private: sqlite3.Connection,
    metadata: dict[str, Any],
) -> str:
    """Check private truth contracts against the operational effects they describe."""
    simulator_config = validate_simulator_config(metadata["resolved_config"]["simulator"])
    episode_config = validate_episode_config(metadata["resolved_config"]["episodes"])
    timezone = ZoneInfo(simulator_config.timeline.business_timezone.value)
    episode_start = datetime.combine(
        simulator_config.timeline.t0_business_date.value,
        time.min,
        tzinfo=timezone,
    ).astimezone(UTC)
    episode_end = episode_start + timedelta(days=episode_config.protocol.duration_days.value)
    assignments = {
        assignment.episode_id: assignment
        for assignment in episode_config.profile("smoke").assignments
    }
    manifest = {
        row["episode_id"]: dict(row)
        for row in private.execute("SELECT * FROM episode_manifest")
    }
    truth_by_episode: dict[str, list[dict[str, Any]]] = {
        episode_id: [] for episode_id in manifest
    }
    for row in private.execute("SELECT * FROM incident_truth"):
        truth = dict(row)
        if truth["episode_id"] not in truth_by_episode:
            _fail(
                f"incident truth {truth['incident_truth_id']} references an episode "
                "without a private manifest"
            )
        truth_by_episode[truth["episode_id"]].append(truth)

    effect_manifest_prefix = "p1.6.incident_effect.v1:"
    effect_manifests: dict[str, dict[str, Any]] = {}
    for row in private.execute(
        "SELECT key, value_json FROM run_metadata WHERE substr(key, 1, ?) = ?",
        (len(effect_manifest_prefix), effect_manifest_prefix),
    ):
        truth_id = row["key"][len(effect_manifest_prefix) :]
        try:
            effect_manifest = json.loads(row["value_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            _fail(f"incident effect reference {truth_id!r} is invalid JSON: {exc}")
        if not isinstance(effect_manifest, dict):
            _fail(f"incident effect reference {truth_id!r} must be a JSON object")
        affected_ids = effect_manifest.get("affected_transaction_ids")
        if (
            not truth_id
            or effect_manifest.get("incident_truth_id") != truth_id
            or not isinstance(effect_manifest.get("episode_id"), str)
            or not isinstance(affected_ids, list)
            or not affected_ids
            or any(not isinstance(txn_id, str) or not txn_id for txn_id in affected_ids)
            or len(affected_ids) != len(set(affected_ids))
        ):
            _fail(f"incident effect reference {truth_id!r} has invalid incident or transaction IDs")
        effect_manifests[truth_id] = effect_manifest

    truth_ids = {
        truth["incident_truth_id"]
        for episode_truths in truth_by_episode.values()
        for truth in episode_truths
    }
    if set(effect_manifests) != truth_ids:
        _fail(
            "private incident effect references differ from incident_truth rows "
            f"(missing={sorted(truth_ids - set(effect_manifests))}, "
            f"orphaned={sorted(set(effect_manifests) - truth_ids)})"
        )

    expected_contracts = {
        "data_pipeline": {
            "cause": "data_pipeline",
            "requires_intervention": 1,
            "rule_covered": 1,
            "mechanisms": {"amount_multiplier_100", "amount_null_burst"},
            "accepted_actions": {"data_ticket", "human_review"},
        },
        "seasonal": {
            "cause": "seasonal",
            "requires_intervention": 0,
            "rule_covered": None,
            "mechanisms": {"one_day_valid_transaction_increase"},
            "accepted_actions": set(),
        },
        "fraud_surge": {
            "cause": "fraud_surge",
            "requires_intervention": 1,
            "rule_covered": 1,
            "mechanisms": {"known_card_testing_surge"},
            "accepted_actions": {"fraud_ops", "human_review"},
        },
        "new_fraud": {
            "cause": "new_fraud",
            "requires_intervention": 1,
            "rule_covered": 0,
            "mechanisms": {
                "social_engineering",
                "silent_concept_drift",
                "new_merchant_sparse_history",
            },
            "accepted_actions": {"human_review", "fraud_ops"},
        },
        "no_material_change": {
            "cause": "no_material_change",
            "requires_intervention": 0,
            "rule_covered": 0,
            "mechanisms": {"new_merchant_sparse_history"},
            "accepted_actions": set(),
        },
        "legit_business_change": {
            "cause": "legit_business_change",
            "requires_intervention": 0,
            "rule_covered": 0,
            "mechanisms": {"legit_business_spike"},
            "accepted_actions": set(),
        },
    }
    verified_incidents = 0
    for episode_id, manifest_entry in manifest.items():
        assignment = assignments.get(episode_id)
        if assignment is None:
            _fail(f"private manifest episode {episode_id} is absent from locked smoke config")
        template = manifest_entry["template"]
        truth_rows = truth_by_episode[episode_id]
        if template == "clean":
            if truth_rows:
                _fail(f"clean episode {episode_id} must not have incident truth")
            continue
        if template not in {"data_pipeline", "seasonal"}:
            _fail(f"unsupported round-one incident template in manifest: {template!r}")
        causes = sorted(truth["cause"] for truth in truth_rows)
        if template == "data_pipeline" and causes != ["data_pipeline"]:
            _fail(f"data_pipeline manifest episode {episode_id} requires one pipeline truth")
        if template == "seasonal" and causes not in (
            ["seasonal"],
            ["fraud_surge", "seasonal"],
        ):
            _fail(
                f"seasonal manifest episode {episode_id} requires seasonal truth, optionally "
                "paired with one fraud_surge truth"
            )
        if template == "seasonal" and len(truth_rows) == 2:
            parents = {truth["parent_scenario"] for truth in truth_rows}
            if len(parents) != 1 or None in parents:
                _fail(
                    f"two-cause episode {episode_id} truths must share a non-null parent_scenario"
                )
            calendar_rows = [
                dict(row)
                for row in operational.execute(
                    "SELECT business_date, expected_effect_json FROM business_calendar "
                    "WHERE episode_id = ? ORDER BY business_date, calendar_event_id",
                    (episode_id,),
                )
            ]
            if len(calendar_rows) != 1:
                _fail(
                    f"two-cause episode {episode_id} must publish exactly the seasonal "
                    "business-calendar component"
                )
            seasonal_truth = next(truth for truth in truth_rows if truth["cause"] == "seasonal")
            seasonal_ids = effect_manifests[seasonal_truth["incident_truth_id"]][
                "affected_transaction_ids"
            ]
            seasonal_placeholders = ", ".join("?" for _ in seasonal_ids)
            seasonal_dates = {
                datetime.fromisoformat(row[0].replace("Z", "+00:00"))
                .astimezone(timezone)
                .date()
                .isoformat()
                for row in operational.execute(
                    "SELECT event_at FROM transactions "
                    f"WHERE txn_id IN ({seasonal_placeholders})",
                    tuple(seasonal_ids),
                )
            }
            try:
                calendar_effect = json.loads(calendar_rows[0]["expected_effect_json"])
                seasonal_change = calendar_effect["txn_count"]["relative_change"]
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                _fail(
                    f"two-cause episode {episode_id} calendar does not declare the seasonal "
                    f"txn_count effect: {exc}"
                )
            if (
                len(seasonal_dates) != 1
                or calendar_rows[0]["business_date"] not in seasonal_dates
                or isinstance(seasonal_change, bool)
                or not isinstance(seasonal_change, (int, float))
                or seasonal_change <= 0
            ):
                _fail(
                    f"two-cause episode {episode_id} calendar must declare only the "
                    "positive seasonal txn_count component"
                )
        for truth in truth_rows:
            expected = expected_contracts.get(truth["cause"])
            if expected is None:
                _fail(
                    f"unsupported incident truth cause {truth['cause']!r} for {episode_id}"
                )
            effect_manifest = effect_manifests[truth["incident_truth_id"]]
            if effect_manifest["episode_id"] != episode_id:
                _fail(
                    f"incident effect reference episode does not match incident truth for {episode_id}"
                )
            affected_ids = effect_manifest["affected_transaction_ids"]
            pre_injection_amounts = effect_manifest.get(
                "pre_injection_amount_by_txn_id"
            )
            is_pipeline = truth["cause"] == "data_pipeline"
            if is_pipeline:
                if (
                    not isinstance(pre_injection_amounts, dict)
                    or set(pre_injection_amounts) != set(affected_ids)
                    or any(
                        type(amount) is not int or amount <= 0
                        for amount in pre_injection_amounts.values()
                    )
                ):
                    _fail(
                        f"data-pipeline effect reference for {episode_id} must preserve each "
                        "positive pre-injection amount in private metadata"
                    )
            elif pre_injection_amounts is not None:
                _fail(
                    f"non-pipeline effect reference for {episode_id} cannot carry pipeline amounts"
                )
            if truth["mechanism"] not in expected["mechanisms"]:
                _fail(
                    f"incident truth mechanism does not match {truth['cause']!r} for {episode_id}"
                )
            affected_rows: list[dict[str, Any]] = []
            for offset in range(0, len(affected_ids), 500):
                batch = affected_ids[offset : offset + 500]
                placeholders = ", ".join("?" for _ in batch)
                affected_rows.extend(
                    dict(row)
                    for row in operational.execute(
                        "SELECT txn_id, episode_id, event_at, available_at, amount "
                        "FROM transactions "
                        f"WHERE txn_id IN ({placeholders})",
                        tuple(batch),
                    )
                )
            actual_ids = {row["txn_id"] for row in affected_rows}
            missing_ids = sorted(set(affected_ids) - actual_ids)
            if missing_ids:
                _fail(
                    f"incident effect reference for {episode_id} points to missing operational "
                    f"transactions: {missing_ids[:5]}"
                )
            if is_pipeline:
                for row in affected_rows:
                    original_amount = pre_injection_amounts[row["txn_id"]]
                    expected_amount = (
                        None
                        if truth["mechanism"] == "amount_null_burst"
                        else original_amount * 100
                    )
                    if row["amount"] != expected_amount:
                        _fail(
                            f"data-pipeline transaction {row['txn_id']} does not match its "
                            "private pre-injection amount and mode"
                        )
            wrong_episode_ids = sorted(
                row["txn_id"] for row in affected_rows if row["episode_id"] != episode_id
            )
            if wrong_episode_ids:
                _fail(
                    f"incident effect reference for {episode_id} points outside its episode: "
                    f"{wrong_episode_ids[:5]}"
                )
            for row in affected_rows:
                event_at = datetime.fromisoformat(row["event_at"].replace("Z", "+00:00"))
                available_at = datetime.fromisoformat(row["available_at"].replace("Z", "+00:00"))
                if not episode_start <= event_at < episode_end:
                    _fail(
                        f"incident affected transaction {row['txn_id']} event_at is outside "
                        f"the episode future window for {episode_id}"
                    )
                if not episode_start <= available_at < episode_end:
                    _fail(
                        f"incident affected transaction {row['txn_id']} available_at is outside "
                        f"the episode future window for {episode_id}"
                    )
            expected_effect_start = min(row["event_at"] for row in affected_rows)
            expected_effect_available = min(row["available_at"] for row in affected_rows)
            expected_effect_end = max(row["event_at"] for row in affected_rows)
            if truth["effect_start_at"] != expected_effect_start:
                _fail(
                    f"incident truth effect_start_at does not match affected operational "
                    f"transactions for {episode_id} (expected {expected_effect_start})"
                )
            if truth["effect_available_at"] != expected_effect_available:
                _fail(
                    f"incident truth effect_available_at does not match affected operational "
                    f"transactions for {episode_id} (expected {expected_effect_available})"
                )
            if (
                truth["effect_end_at"] is not None
                and truth["effect_end_at"] != expected_effect_end
            ):
                _fail(
                    f"incident truth effect_end_at does not match affected operational "
                    f"transactions for {episode_id} (expected {expected_effect_end})"
                )
            if truth["requires_intervention"] != expected["requires_intervention"]:
                _fail(
                    f"incident truth requires_intervention does not match {truth['cause']!r} "
                    f"for {episode_id}"
                )
            expected_rule_covered = expected["rule_covered"]
            if truth["cause"] == "seasonal":
                local_dates = {
                    datetime.fromisoformat(row["event_at"].replace("Z", "+00:00"))
                    .astimezone(timezone)
                    .date()
                    .isoformat()
                    for row in affected_rows
                }
                if len(local_dates) != 1:
                    _fail(f"seasonal truth for {episode_id} must affect exactly one business date")
                date_placeholders = ", ".join("?" for _ in local_dates)
                calendar_dates = {
                    row[0]
                    for row in operational.execute(
                        "SELECT business_date FROM business_calendar "
                        f"WHERE episode_id = ? AND business_date IN ({date_placeholders})",
                        (episode_id, *sorted(local_dates)),
                    )
                }
                expected_rule_covered = int(bool(calendar_dates))
            if truth["rule_covered"] != expected_rule_covered:
                _fail(
                    f"incident truth rule_covered does not match {truth['cause']!r} for {episode_id}"
                )
            try:
                actions = json.loads(truth["accepted_actions_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                _fail(f"incident truth accepted_actions_json is invalid for {episode_id}: {exc}")
            if (
                not isinstance(actions, list)
                or any(not isinstance(action, str) for action in actions)
                or len(actions) != len(set(actions))
                or set(actions) != expected["accepted_actions"]
            ):
                _fail(f"incident truth accepted_actions do not match docs/data.md section 5 for {episode_id}")

            time_fields = (
                "effect_start_at",
                "effect_available_at",
                "intervention_start_at",
                "deadline_at",
            )
            parsed_times: dict[str, datetime] = {}
            for field in time_fields:
                value = truth[field]
                if utc_timestamp(value) != value:
                    _fail(f"incident truth {field} must use fixed-width UTC for {episode_id}")
                parsed_times[field] = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if not parsed_times["effect_start_at"] <= parsed_times["effect_available_at"]:
                _fail(f"incident truth effect_start_at follows effect_available_at for {episode_id}")
            if parsed_times["intervention_start_at"] != parsed_times["effect_available_at"]:
                _fail(f"incident truth intervention_start_at must equal effect_available_at for {episode_id}")
            if parsed_times["intervention_start_at"] > parsed_times["deadline_at"]:
                _fail(f"incident truth intervention_start_at follows deadline_at for {episode_id}")
            if parsed_times["deadline_at"] != (
                parsed_times["intervention_start_at"]
                + timedelta(days=MVP_INCIDENT_DEADLINE_DAYS)
            ):
                _fail(
                    f"incident truth deadline_at must equal intervention_start_at + "
                    f"{MVP_INCIDENT_DEADLINE_DAYS} days for {episode_id}"
                )
            effect_end = truth["effect_end_at"]
            if effect_end is not None:
                if utc_timestamp(effect_end) != effect_end:
                    _fail(f"incident truth effect_end_at must use fixed-width UTC for {episode_id}")
                if datetime.fromisoformat(effect_end.replace("Z", "+00:00")) < parsed_times["effect_start_at"]:
                    _fail(f"incident truth effect_end_at precedes effect_start_at for {episode_id}")
            verified_incidents += 1
    _validate_parent_scenarios(truth_by_episode)
    _validate_p16_fraud_groups(operational, private, truth_by_episode, effect_manifests)
    return f"{verified_incidents} incident truth rows match locked templates and docs/data.md section 5"


def _validate_parent_scenarios(
    truth_by_episode: dict[str, list[dict[str, Any]]],
) -> None:
    """Require parent components to be the supported overlapping pair in one episode."""
    parents: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for episode_id, truth_rows in truth_by_episode.items():
        for truth in truth_rows:
            parent = truth.get("parent_scenario")
            if parent is None:
                continue
            if not isinstance(parent, str) or not parent.strip():
                _fail(
                    f"incident truth {truth['incident_truth_id']} parent_scenario must be non-empty text"
                )
            parents.setdefault((episode_id, parent), []).append(truth)
    for (episode_id, parent), members in parents.items():
        if len(members) != 2:
            _fail(
                f"parent scenario {parent!r} in episode {episode_id} must contain exactly "
                "two truth components"
            )
        causes = [truth["cause"] for truth in members]
        if set(causes) != {"fraud_surge", "seasonal"}:
            _fail(
                f"parent scenario {parent!r} in episode {episode_id} must pair one fraud_surge "
                "and one seasonal truth"
            )
        intervals: list[tuple[datetime, datetime]] = []
        for truth in members:
            try:
                start = datetime.fromisoformat(truth["effect_start_at"].replace("Z", "+00:00"))
                end_value = truth.get("effect_end_at") or truth["effect_start_at"]
                end = datetime.fromisoformat(end_value.replace("Z", "+00:00"))
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                _fail(
                    f"parent scenario {parent!r} in episode {episode_id} has invalid effect interval: {exc}"
                )
            if start.tzinfo is None or end.tzinfo is None or end < start:
                _fail(
                    f"parent scenario {parent!r} in episode {episode_id} has invalid effect interval"
                )
            intervals.append((start, end))
        if max(interval[0] for interval in intervals) > min(interval[1] for interval in intervals):
            _fail(
                f"parent scenario {parent!r} in episode {episode_id} component effect windows must overlap"
            )


def _validate_p16_fraud_groups(
    operational: sqlite3.Connection,
    private: sqlite3.Connection,
    truth_by_episode: dict[str, list[dict[str, Any]]],
    effect_manifests: dict[str, dict[str, Any]],
) -> None:
    """Check P1.6-generated fraud labels against their private incident effects."""
    truths_by_id = {
        truth["incident_truth_id"]: truth
        for rows in truth_by_episode.values()
        for truth in rows
    }
    seen_transaction_ids: set[str] = set()
    for row in private.execute(
        "SELECT key, value_json FROM run_metadata "
        "WHERE substr(key, 1, ?) = ?",
        (len("p1.6.fraud_groups.v1:"), "p1.6.fraud_groups.v1:"),
    ):
        episode_id = row["key"].split(":", 1)[1]
        try:
            metadata = json.loads(row["value_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            _fail(f"P1.6 fraud group metadata for {episode_id!r} is invalid JSON: {exc}")
        groups = metadata.get("groups") if isinstance(metadata, dict) else None
        if (
            not isinstance(metadata, dict)
            or metadata.get("algorithm") != "p1.6-fraud-groups-v1"
            or metadata.get("episode_id") != episode_id
            or type(metadata.get("seed")) is not int
            or not isinstance(groups, list)
            or not groups
        ):
            _fail(f"P1.6 fraud group metadata for {episode_id!r} is malformed")
        for group in groups:
            if not isinstance(group, dict):
                _fail(f"P1.6 fraud group for {episode_id!r} must be an object")
            truth_id = group.get("incident_truth_id")
            mechanism = group.get("mechanism")
            transaction_ids = group.get("transaction_ids")
            truth = truths_by_id.get(truth_id)
            effect = effect_manifests.get(truth_id)
            if (
                group.get("role") != "fraud"
                or not isinstance(truth_id, str)
                or truth is None
                or truth["episode_id"] != episode_id
                or effect is None
                or effect.get("episode_id") != episode_id
                or not isinstance(transaction_ids, list)
                or not transaction_ids
                or any(not isinstance(item, str) or not item for item in transaction_ids)
                or len(transaction_ids) != len(set(transaction_ids))
                or set(transaction_ids) != set(effect.get("affected_transaction_ids", []))
            ):
                _fail(f"P1.6 fraud group does not match incident truth {truth_id!r}")
            expected_truth_mechanism = {
                "fraud_surge": "known_card_testing_surge",
                "silent_concept_drift": "silent_concept_drift",
                "sparse_new_merchant": "new_merchant_sparse_history",
            }.get(mechanism)
            if expected_truth_mechanism is None or truth["mechanism"] != expected_truth_mechanism:
                _fail(f"P1.6 fraud group mechanism does not match incident truth {truth_id!r}")
            if any(item in seen_transaction_ids for item in transaction_ids):
                _fail(f"P1.6 fraud transaction appears in more than one fraud group: {truth_id!r}")
            seen_transaction_ids.update(transaction_ids)
            for offset in range(0, len(transaction_ids), 500):
                batch = transaction_ids[offset : offset + 500]
                placeholders = ", ".join("?" for _ in batch)
                rows = operational.execute(
                    "SELECT txn_id, episode_id FROM transactions "
                    f"WHERE txn_id IN ({placeholders})",
                    tuple(batch),
                ).fetchall()
                if {item["txn_id"] for item in rows} != set(batch) or any(
                    item["episode_id"] != episode_id for item in rows
                ):
                    _fail(f"P1.6 fraud group references missing or cross-episode rows: {truth_id!r}")


def _pipeline_effect_amounts(
    private: sqlite3.Connection | None,
) -> tuple[dict[str, int], set[str], set[str]]:
    if private is None:
        return {}, set(), set()

    truth_by_id = {
        row["incident_truth_id"]: dict(row)
        for row in private.execute("SELECT * FROM incident_truth")
    }
    prefix = "p1.6.incident_effect.v1:"
    original_amount_by_txn_id: dict[str, int] = {}
    pipeline_affected_ids: set[str] = set()
    null_mode_ids: set[str] = set()
    for metadata_row in private.execute(
        "SELECT key, value_json FROM run_metadata WHERE substr(key, 1, ?) = ?",
        (len(prefix), prefix),
    ):
        truth_id = metadata_row["key"][len(prefix) :]
        truth = truth_by_id.get(truth_id)
        if truth is None or truth["cause"] != "data_pipeline":
            continue
        try:
            effect_manifest = json.loads(metadata_row["value_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            _fail(f"private data-pipeline effect reference {truth_id!r} is invalid JSON: {exc}")
        if not isinstance(effect_manifest, dict):
            _fail(f"private data-pipeline effect reference {truth_id!r} must be an object")
        affected_ids = effect_manifest.get("affected_transaction_ids")
        original_amounts = effect_manifest.get("pre_injection_amount_by_txn_id")
        if (
            effect_manifest.get("incident_truth_id") != truth_id
            or effect_manifest.get("episode_id") != truth["episode_id"]
            or not isinstance(affected_ids, list)
            or not affected_ids
            or any(not isinstance(txn_id, str) or not txn_id for txn_id in affected_ids)
            or len(affected_ids) != len(set(affected_ids))
            or not isinstance(original_amounts, dict)
            or set(original_amounts) != set(affected_ids)
        ):
            _fail(f"private data-pipeline effect reference {truth_id!r} lacks original amounts")
        for txn_id, amount in original_amounts.items():
            if type(amount) is not int or amount <= 0:
                _fail(f"private data-pipeline effect reference {truth_id!r} has an invalid original amount")
            if txn_id in original_amount_by_txn_id:
                _fail(f"transaction {txn_id} is listed in multiple data-pipeline effects")
            original_amount_by_txn_id[txn_id] = amount
            pipeline_affected_ids.add(txn_id)
        if truth["mechanism"] == "amount_null_burst":
            null_mode_ids.update(affected_ids)
        elif truth["mechanism"] != "amount_multiplier_100":
            _fail(f"data-pipeline truth {truth_id!r} has an unsupported mechanism")
    return original_amount_by_txn_id, pipeline_affected_ids, null_mode_ids


def _collect_contract_violations(
    operational: sqlite3.Connection,
    private: sqlite3.Connection | None = None,
) -> str:
    violations: list[str] = []
    original_amount_by_txn_id, pipeline_affected_ids, null_mode_ids = (
        _pipeline_effect_amounts(private)
    )
    registered = {
        row[0] for row in operational.execute("SELECT episode_id FROM episodes")
    }
    transactions = {
        row["txn_id"]: dict(row)
        for row in operational.execute("SELECT * FROM transactions")
    }
    entity_episodes: dict[str, dict[str, str]] = {}
    for table, key in (("customers", "customer_id"), ("merchants", "merchant_id")):
        entity_episodes[table] = {
            row[key]: row["episode_id"] for row in operational.execute(f'SELECT * FROM "{table}"')
        }

    for table, timestamp_columns in OPERATIONAL_TIMESTAMP_COLUMNS.items():
        rows = operational.execute(f'SELECT * FROM "{table}"').fetchall()
        for row in rows:
            row_dict = dict(row)
            for column in timestamp_columns:
                value = row_dict.get(column)
                try:
                    if not isinstance(value, str) or utc_timestamp(value) != value:
                        raise StorageError("timestamp must use fixed-width UTC Z format")
                except StorageError as exc:
                    violations.append(f"{table}.{column} {value!r}: {exc}")
            if "event_at" in row_dict and "available_at" in row_dict:
                if row_dict["available_at"] < row_dict["event_at"]:
                    violations.append(
                        f"{table} row {row_dict.get('event_id', row_dict.get('txn_id', row_dict.get('customer_id', row_dict.get('merchant_id', row_dict.get('calendar_event_id')))))} "
                        "has available_at before event_at"
                    )
            episode_id = row_dict.get("episode_id")
            if episode_id not in registered:
                violations.append(f"{table} row references unknown episode {episode_id!r}")
            elif "event_at" in row_dict and (
                (episode_id == "base" and row_dict["event_at"] >= T0_UTC_TEXT)
                or (episode_id != "base" and row_dict["event_at"] < T0_UTC_TEXT)
            ):
                violations.append(f"{table} row is on the wrong side of T0 for {episode_id}")

    for txn_id, row in transactions.items():
        if row["amount"] is None:
            if txn_id not in null_mode_ids:
                violations.append(
                    f"transaction {txn_id} has NULL amount without a private data-pipeline null effect"
                )
        elif txn_id in null_mode_ids:
            violations.append(
                f"transaction {txn_id} is listed for a data-pipeline null effect but amount is not NULL"
            )
        elif txn_id in pipeline_affected_ids:
            original_amount = original_amount_by_txn_id[txn_id]
            if row["amount"] != original_amount * 100:
                violations.append(
                    f"transaction {txn_id} does not match its private data-pipeline amount effect"
                )
        if row["created_at"] != row["event_at"]:
            violations.append(f"transaction {txn_id} has created_at != event_at")
        event_at = row["event_at"]
        episode_id = row["episode_id"]
        if (episode_id == "base" and event_at >= T0_UTC_TEXT) or (
            episode_id != "base" and event_at < T0_UTC_TEXT
        ):
            violations.append(f"transaction {txn_id} is on the wrong side of T0 for {episode_id}")
        customer_episode = entity_episodes["customers"].get(row["customer_id"])
        merchant_id = row["merchant_id"]
        recipient_customer_id = row["recipient_customer_id"]
        merchant_episode = (
            entity_episodes["merchants"].get(merchant_id)
            if merchant_id is not None
            else None
        )
        recipient_episode = (
            entity_episodes["customers"].get(recipient_customer_id)
            if recipient_customer_id is not None
            else None
        )
        if customer_episode not in {"base", episode_id}:
            violations.append(f"transaction {txn_id} references a customer from another episode")
        if (merchant_id is None) == (recipient_customer_id is None):
            violations.append(
                f"transaction {txn_id} must have exactly one merchant or account recipient"
            )
        if row["channel"] not in {"qr", "wallet", "card"}:
            violations.append(f"transaction {txn_id} has an unsupported channel")
        for column in ("device_id", "card_token"):
            value = row.get(column)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                violations.append(f"transaction {txn_id} has an invalid {column}")
        if row.get("card_token") is not None and row["channel"] != "card":
            violations.append(f"transaction {txn_id} has a card_token outside the card channel")
        if merchant_id is not None and merchant_episode not in {"base", episode_id}:
            violations.append(f"transaction {txn_id} references a merchant from another episode")
        if recipient_customer_id is not None:
            if recipient_episode not in {"base", episode_id}:
                violations.append(
                    f"transaction {txn_id} references a recipient from another episode"
                )
            if recipient_customer_id == row["customer_id"]:
                violations.append(f"transaction {txn_id} transfers to its sending account")
            if row["channel"] != "wallet":
                violations.append(f"account transfer {txn_id} must use the wallet channel")

    event_rows = [
        dict(row) for row in operational.execute("SELECT * FROM transaction_events")
    ]
    events_by_txn_id: dict[str, list[dict[str, Any]]] = {}
    for row in event_rows:
        events_by_txn_id.setdefault(row["txn_id"], []).append(row)

    adjustments_by_txn_id: dict[str, list[dict[str, Any]]] = {}
    for row in event_rows:
        parent = transactions.get(row["txn_id"])
        if parent is None:
            violations.append(f"transaction event {row['event_id']} has no transaction parent")
            continue
        episode_id = row["episode_id"]
        event_at = row["event_at"]
        if (episode_id == "base" and event_at >= T0_UTC_TEXT) or (
            episode_id != "base" and event_at < T0_UTC_TEXT
        ):
            violations.append(f"transaction event {row['event_id']} is on the wrong side of T0")
        if (episode_id == "base" and parent["episode_id"] != "base") or (
            episode_id != "base" and parent["episode_id"] not in {"base", episode_id}
        ):
            violations.append(f"transaction event {row['event_id']} crosses episode boundary")
        if row["event_type"] in {"refunded", "reversed"}:
            adjustments_by_txn_id.setdefault(row["txn_id"], []).append(row)
            amount = row["amount"]
            original_amount = original_amount_by_txn_id.get(
                row["txn_id"], parent["amount"]
            )
            if (
                isinstance(amount, bool)
                or not isinstance(amount, int)
                or amount <= 0
                or isinstance(original_amount, bool)
                or not isinstance(original_amount, int)
                or amount > original_amount
            ):
                violations.append(
                    f"transaction event {row['event_id']} has an invalid refund/reversal amount"
                )
            if event_at <= parent["created_at"]:
                violations.append(
                    f"transaction event {row['event_id']} must follow its original transaction"
                )
            settled_before = any(
                candidate["event_type"] == "settled"
                and candidate["event_at"] < event_at
                and candidate["available_at"] <= row["available_at"]
                for candidate in events_by_txn_id[row["txn_id"]]
            )
            if not settled_before:
                violations.append(
                    f"transaction event {row['event_id']} adjustment requires a prior available settlement"
                )
        elif row["amount"] is not None:
            violations.append(
                f"transaction event {row['event_id']} amount must be NULL for {row['event_type']}"
            )

    for txn_id, adjustments in adjustments_by_txn_id.items():
        if len(adjustments) > 1:
            violations.append(f"transaction {txn_id} has more than one refund/reversal adjustment")
        original_amount = original_amount_by_txn_id.get(
            txn_id, transactions[txn_id]["amount"]
        )
        if all(type(row["amount"]) is int and row["amount"] > 0 for row in adjustments):
            if type(original_amount) is int and sum(
                row["amount"] for row in adjustments
            ) > original_amount:
                violations.append(
                    f"transaction {txn_id} refund/reversal total exceeds its original amount"
                )

    for row in operational.execute("SELECT * FROM label_events"):
        parent = transactions.get(row["txn_id"])
        if parent is None:
            violations.append(f"label event {row['event_id']} has no transaction parent")
            continue
        if row["event_at"] != parent["event_at"]:
            violations.append(f"label event {row['event_id']} event_at differs from its transaction")
        if row["reported_at"] < row["event_at"] or row["available_at"] < row["reported_at"]:
            violations.append(f"label event {row['event_id']} violates report/availability ordering")
        if row["label_status"] != "confirmed" or row["label"] not in {"fraud", "legit"}:
            violations.append(f"label event {row['event_id']} stores a non-confirmed/pending state")

    for row in operational.execute("SELECT * FROM business_calendar"):
        try:
            local_time = datetime.fromisoformat(row["event_at"].replace("Z", "+00:00"))
            timezone_name = "Asia/Ho_Chi_Minh"
            local_time = local_time.astimezone(ZoneInfo(timezone_name))
            if local_time.date().isoformat() != row["business_date"] or any(
                (local_time.hour, local_time.minute, local_time.second, local_time.microsecond)
            ):
                violations.append(f"calendar event {row['calendar_event_id']} does not mark local midnight")
        except (TypeError, ValueError):
            violations.append(f"calendar event {row['calendar_event_id']} has invalid business date")

    expected_episode_ids = registered - {"base"}
    if not expected_episode_ids:
        violations.append("smoke run must contain registered non-base episodes")
    for table, column in (
        ("customers", "customer_id"),
        ("merchants", "merchant_id"),
        ("transactions", "txn_id"),
        ("transaction_events", "event_id"),
        ("label_events", "event_id"),
        ("business_calendar", "calendar_event_id"),
    ):
        for row in operational.execute(f'SELECT "{column}" FROM "{table}"'):
            identifier = str(row[0]).lower()
            tokens = set(re.split(r"[^a-z0-9]+", identifier.replace("_", "-")))
            if tokens & _RESERVED_ID_TOKENS or any(
                forbidden in identifier
                for forbidden in ("card_testing", "data_pipeline", "fraud_surge", "truth-")
            ):
                violations.append(f"{table}.{column} encodes private scenario content: {identifier}")
    for episode_id in expected_episode_ids:
        if not re.fullmatch(r"ep-[0-9]{4}", episode_id):
            violations.append(f"operational episode ID is not neutral: {episode_id!r}")

    if violations:
        _fail("; ".join(violations[:20]))
    return (
        "timestamps and available_at order, base/episode T0 boundaries, entity/event/label "
        "relationships, calendar dates, and neutral IDs are valid"
    )


def _validate_monitoring_outputs(operational: sqlite3.Connection) -> str:
    """Validate P3 rows against their P3 source rows without reading private truth."""
    violations: list[str] = []
    registered = {
        row[0] for row in operational.execute("SELECT episode_id FROM episodes")
    }
    metrics_by_key: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    alerts_by_id: dict[str, dict[str, Any]] = {}
    incidents_by_key: set[tuple[str, str]] = set()

    def check_neutral_text(table: str, row_id: str, values: tuple[Any, ...]) -> None:
        for value in values:
            if not isinstance(value, str):
                continue
            tokens = set(re.split(r"[^a-z0-9]+", value.lower()))
            if tokens & _MONITORING_PRIVATE_MARKERS:
                violations.append(
                    f"{table} row {row_id} encodes private scenario or truth content"
                )
                return

    def check_private_fields(table: str, row_id: str, values: tuple[Any, ...]) -> None:
        for value in values:
            if not isinstance(value, str):
                continue
            text = value.casefold()
            if any(pattern.search(text) for pattern in _MONITORING_PRIVATE_FIELD_PATTERNS):
                violations.append(
                    f"{table} row {row_id} contains a private truth or injector field name"
                )
                return

    metrics = operational.execute("SELECT * FROM daily_metrics").fetchall()
    for source in metrics:
        row = dict(source)
        metric_id = str(row["metric_id"])
        episode_id = str(row["episode_id"])
        as_of_time = str(row["as_of_time"])
        if episode_id == "base" or episode_id not in registered:
            violations.append(f"daily_metrics row {metric_id} has an invalid episode")
        if row["event_at"] != as_of_time or row["available_at"] != as_of_time:
            violations.append(
                f"daily_metrics row {metric_id} event_at and available_at must equal as_of_time"
            )
        try:
            flags = json.loads(row["flags"])
        except (TypeError, json.JSONDecodeError):
            flags = None
        if not isinstance(flags, list) or any(not isinstance(flag, str) for flag in flags):
            violations.append(f"daily_metrics row {metric_id} flags must be a JSON string array")
        elif any(
            flag not in _MONITORING_QUALITY_FLAGS
            and _MONITORING_COUNT_FLAG.fullmatch(flag) is None
            for flag in flags
        ):
            violations.append(
                f"daily_metrics row {metric_id} flags contain unsupported quality flags"
            )
        check_neutral_text(
            "daily_metrics",
            metric_id,
            (metric_id, row["feature"], row["segment"], row["reference_window"], row["flags"]),
        )
        check_private_fields("daily_metrics", metric_id, tuple(row.values()))
        key = (
            episode_id,
            as_of_time,
            metric_id,
            str(row["feature"]),
            str(row["segment"]),
        )
        metrics_by_key[key] = row

    alerts = operational.execute("SELECT * FROM alerts").fetchall()
    for source in alerts:
        row = dict(source)
        alert_id = str(row["alert_id"])
        episode_id = str(row["episode_id"])
        as_of_time = str(row["as_of_time"])
        if episode_id == "base" or episode_id not in registered:
            violations.append(f"alert {alert_id} has an invalid episode")
        if row["event_at"] != as_of_time or row["available_at"] != as_of_time:
            violations.append(
                f"alert {alert_id} event_at and available_at must equal as_of_time"
            )
        identity = {
            "schema_version": 1,
            "episode_id": episode_id,
            "as_of_time": as_of_time,
            "metric": row["metric"],
            "feature": row["feature"],
            "segment": row["segment"],
        }
        expected_id = "alert-" + _canonical_json_hash(identity)[:24]
        if alert_id != expected_id:
            violations.append(f"alert {alert_id} is not the neutral ID for its episode and scope")
        metric_key = (
            episode_id,
            as_of_time,
            str(row["metric"]),
            str(row["feature"]),
            str(row["segment"]),
        )
        metric = metrics_by_key.get(metric_key)
        if metric is None:
            violations.append(f"alert {alert_id} has no same-episode daily_metrics source")
        elif (
            metric["value"] != row["value"]
            or metric["reference_window"] != row["reference"]
        ):
            violations.append(f"alert {alert_id} differs from its daily_metrics source")
        check_neutral_text(
            "alerts",
            alert_id,
            (row["metric"], row["feature"], row["segment"], row["reference"]),
        )
        check_private_fields("alerts", alert_id, tuple(row.values()))
        alerts_by_id[alert_id] = row

    incidents = operational.execute("SELECT * FROM incidents").fetchall()
    for source in incidents:
        row = dict(source)
        incident_id = str(row["incident_id"])
        episode_id = str(row["episode_id"])
        opened_at = str(row["opened_at"])
        event_at = str(row["event_at"])
        available_at = str(row["available_at"])
        if episode_id == "base" or episode_id not in registered:
            violations.append(f"incident {incident_id} has an invalid episode")
        if not opened_at <= event_at <= available_at:
            violations.append(
                f"incident {incident_id} must satisfy opened_at <= event_at <= available_at"
            )
        incidents_by_key.add((incident_id, episode_id))
        try:
            alert_ids = json.loads(row["alert_ids_json"])
        except (TypeError, json.JSONDecodeError):
            alert_ids = None
        if (
            not isinstance(alert_ids, list)
            or not alert_ids
            or any(not isinstance(alert_id, str) for alert_id in alert_ids)
            or len(alert_ids) != len(set(alert_ids))
        ):
            violations.append(f"incident {incident_id} alert_ids_json must be a unique nonempty string array")
            continue

        known_alerts = [alerts_by_id[alert_id] for alert_id in alert_ids if alert_id in alerts_by_id]
        expected_alert_order = [
            alert["alert_id"]
            for alert in sorted(
                known_alerts,
                key=lambda alert: (alert["as_of_time"], alert["alert_id"]),
            )
        ]
        if len(known_alerts) == len(alert_ids) and alert_ids != expected_alert_order:
            violations.append(
                f"incident {incident_id} alert_ids_json is not ordered by as_of_time then alert_id"
            )

        first_alert = alerts_by_id.get(alert_ids[0])
        for alert_id in alert_ids:
            alert = alerts_by_id.get(alert_id)
            if alert is None:
                violations.append(f"incident {incident_id} references missing alert {alert_id}")
                continue
            if alert["episode_id"] != episode_id:
                violations.append(f"incident {incident_id} references an alert from another episode")
            if alert["feature"] != row["feature"] or alert["segment"] != row["segment"]:
                violations.append(f"incident {incident_id} references an alert from another scope")
            if alert["available_at"] > available_at:
                violations.append(
                    f"incident {incident_id} snapshot is available before alert {alert_id}"
                )
        if first_alert is not None:
            if first_alert["episode_id"] != episode_id:
                violations.append(f"incident {incident_id} first alert belongs to another episode")
            if row["opened_at"] != first_alert["as_of_time"]:
                violations.append(f"incident {incident_id} opened_at differs from its first alert")
            identity = {
                "schema_version": 1,
                "episode_id": episode_id,
                "first_alert_id": alert_ids[0],
                "feature": row["feature"],
                "segment": row["segment"],
                "parent_incident_id": row["parent_incident_id"],
            }
            expected_id = "incident-" + _canonical_json_hash(identity)[:24]
            if incident_id != expected_id:
                violations.append(f"incident {incident_id} is not the neutral ID for its scope")
        if row["parent_incident_id"] is not None:
            parent_key = (str(row["parent_incident_id"]), episode_id)
            if parent_key not in incidents_by_key and not operational.execute(
                "SELECT 1 FROM incidents WHERE incident_id = ? AND episode_id = ? LIMIT 1",
                parent_key,
            ).fetchone():
                violations.append(f"incident {incident_id} references a parent from another episode or no parent")
        check_neutral_text(
            "incidents",
            incident_id,
            (row["feature"], row["segment"]),
        )
        check_private_fields("incidents", incident_id, tuple(row.values()))

    if violations:
        _fail("; ".join(violations[:20]))
    return (
        f"{len(metrics)} daily metrics, {len(alerts)} alerts, and {len(incidents)} incidents "
        "have matching same-episode sources, as-of timestamps, neutral IDs, and no truth content"
    )


def _validate_decisions(operational: sqlite3.Connection) -> str:
    """Validate operational decision rows without consulting private truth."""
    violations: list[str] = []
    registered = {
        row[0] for row in operational.execute("SELECT episode_id FROM episodes")
    }
    rows = operational.execute("SELECT * FROM decisions ORDER BY decision_id").fetchall()
    decision_columns = tuple(
        row[1] for row in operational.execute('PRAGMA table_info("decisions")')
    )
    expected_columns = (
        "decision_id",
        "incident_id",
        "episode_id",
        "decision_at",
        "as_of_time",
        "event_at",
        "available_at",
        "source",
        "runbook",
        "cause",
        "evidence_status",
        "action",
        "scope_json",
        "checklist_results_json",
        "tool_trace_ids_json",
        "conclusion",
        "reason",
        "close_score",
        "policy_version",
        "model_slug",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cost_usd",
    )
    if decision_columns != expected_columns:
        _fail("decisions columns differ from the operational decision contract")

    private_json_keys = {
        "truth",
        "truth_bearing",
        "incident_truth",
        "incident_truth_id",
        "mechanism",
        "template",
        "seed",
        "accepted_actions",
        "accepted_actions_json",
        "affected_entities_json",
        "affected_features_json",
        "affected_segments_json",
        "effect_available_at",
        "effect_end_at",
        "effect_start_at",
        "intervention_start_at",
        "deadline_at",
        "parent_scenario",
        "requires_intervention",
        "rule_covered",
    }

    def nested_keys(value: Any):
        if isinstance(value, dict):
            for key, nested in value.items():
                yield str(key)
                yield from nested_keys(nested)
        elif isinstance(value, list):
            for nested in value:
                yield from nested_keys(nested)

    for source in rows:
        row = dict(source)
        decision_id = str(row["decision_id"])
        episode_id = str(row["episode_id"])
        if episode_id == "base" or episode_id not in registered:
            violations.append(f"decision {decision_id} has an invalid episode")
        if not re.fullmatch(r"decision-[0-9a-f]{24}", decision_id):
            violations.append(f"decision {decision_id!r} is not a neutral deterministic ID")
        if not re.fullmatch(r"incident-[0-9a-f]{24}", str(row["incident_id"])):
            violations.append(f"decision {decision_id} references a non-neutral incident ID")
        times = tuple(
            row[field]
            for field in ("decision_at", "as_of_time", "event_at", "available_at")
        )
        try:
            normalized_times = tuple(utc_timestamp(value) for value in times)
        except StorageError:
            normalized_times = ()
        if not normalized_times or len(set(normalized_times)) != 1 or normalized_times != times:
            violations.append(
                f"decision {decision_id} temporal columns must use one fixed-width UTC instant"
            )
        identity = {
            "schema_version": 1,
            "decision": normalize_decision_real_columns(
                {key: row[key] for key in decision_columns if key != "decision_id"}
            ),
        }
        expected_id = "decision-" + _canonical_json_hash(identity)[:24]
        if decision_id != expected_id:
            violations.append(f"decision {decision_id} does not match its neutral operational content")
        if not operational.execute(
            "SELECT 1 FROM incidents WHERE incident_id = ? AND episode_id = ? "
            "AND available_at <= ? LIMIT 1",
            (row["incident_id"], episode_id, row["decision_at"]),
        ).fetchone():
            violations.append(
                f"decision {decision_id} has no same-episode incident snapshot available at decision_at"
            )
        if row["source"] not in {"rule", "agent"}:
            violations.append(f"decision {decision_id} has an unsupported source")
        elif row["source"] == "rule" and row["model_slug"] is not None:
            violations.append(f"rule decision {decision_id} must have NULL model_slug")
        if row["evidence_status"] not in {"sufficient", "insufficient", "conflicting"}:
            violations.append(f"decision {decision_id} has an unsupported evidence status")
        if row["action"] not in {"close", "recheck", "data_ticket", "fraud_ops", "human_review"}:
            violations.append(f"decision {decision_id} has an unsupported action")
        json_types = {
            "scope_json": dict,
            "checklist_results_json": dict,
            "tool_trace_ids_json": list,
        }
        for column, expected_type in json_types.items():
            try:
                decoded = json.loads(row[column])
            except (TypeError, json.JSONDecodeError):
                decoded = None
            if not isinstance(decoded, expected_type):
                violations.append(f"decision {decision_id} {column} has the wrong JSON type")
                continue
            if set(nested_keys(decoded)) & private_json_keys:
                violations.append(f"decision {decision_id} stores a private truth field")
        try:
            scope = json.loads(row["scope_json"])
            checklist = json.loads(row["checklist_results_json"])
            trace_ids = json.loads(row["tool_trace_ids_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if set(scope) != {"feature", "segment", "start_at", "end_at"} or any(
            value is not None and not isinstance(value, str) for value in scope.values()
        ):
            violations.append(f"decision {decision_id} has an invalid scope_json object")
        for field in ("start_at", "end_at"):
            if isinstance(scope.get(field), str):
                try:
                    if utc_timestamp(scope[field]) != scope[field]:
                        raise StorageError("timestamp is not normalized UTC")
                except StorageError:
                    violations.append(f"decision {decision_id} scope_json.{field} is invalid")
        valid_checklist_statuses = {
            "pass",
            "fail",
            "pending",
            "conflicting",
            "error",
            "not_applicable",
        }
        valid_checklist_fields = {
            "status",
            "reason",
            "scope_match",
            "window_complete",
            "requires_human_review",
            "details",
        }
        if any(
            not isinstance(name, str)
            or not isinstance(item, dict)
            or set(item) - valid_checklist_fields
            or item.get("status") not in valid_checklist_statuses
            or ("reason" in item and not isinstance(item["reason"], str))
            or any(
                key in item and not isinstance(item[key], bool)
                for key in ("scope_match", "window_complete", "requires_human_review")
            )
            for name, item in checklist.items()
        ):
            violations.append(f"decision {decision_id} has invalid checklist result entries")
        if any(
            not isinstance(item, str) or not item.strip() for item in trace_ids
        ):
            violations.append(f"decision {decision_id} has invalid tool trace IDs")
        score = row["close_score"]
        if score is not None and (
            not isinstance(score, (int, float))
            or not math.isfinite(float(score))
            or not 0 <= float(score) <= 1
        ):
            violations.append(f"decision {decision_id} has an invalid close_score")

    if violations:
        _fail("; ".join(violations[:20]))
    return f"{len(rows)} decisions have neutral IDs, same-episode UTC as-of fields, and no truth columns"


def _validate_asof(operational: sqlite3.Connection) -> str:
    episodes = [
        row[0]
        for row in operational.execute(
            "SELECT episode_id FROM episodes WHERE episode_id <> 'base' ORDER BY episode_id"
        )
    ]
    if not episodes:
        _fail("no non-base episode to validate through the as-of reader")
    t0 = T0_UTC_TEXT
    end = "2026-03-02T17:00:00.000000Z"
    compared = 0
    for episode_id in episodes:
        for table in sorted(AS_OF_TABLES):
            for as_of_time in (t0, end):
                rows = read_as_of(
                    operational,
                    table,
                    episode_id=episode_id,
                    t0=t0,
                    as_of_time=as_of_time,
                )
                for row in rows:
                    if row["available_at"] > as_of_time:
                        _fail(f"as-of reader returned unavailable {table} row {row}")
                    if row["episode_id"] not in {"base", episode_id}:
                        _fail(f"as-of reader returned a cross-episode {table} row")
                    if row["episode_id"] == "base" and row["event_at"] >= t0:
                        _fail(f"as-of reader returned base {table} data at/after T0")
                    if row["episode_id"] == episode_id and row["event_at"] < t0:
                        _fail(f"as-of reader returned episode {table} data before T0")
                primary_key = _primary_key(operational, table)
                selected_keys = ", ".join(f'"{column}"' for column in primary_key)
                expected = {
                    tuple(row[column] for column in primary_key)
                    for row in operational.execute(
                        f'SELECT {selected_keys} FROM "{table}" WHERE available_at <= ? '
                        "AND ((episode_id = 'base' AND event_at < ?) OR episode_id = ?)",
                        (as_of_time, t0, episode_id),
                    )
                }
                actual = {
                    tuple(row.get(column) for column in primary_key)
                    for row in rows
                }
                if actual != expected:
                    _fail(
                        f"{table} as-of selection differs for episode={episode_id} "
                        f"as_of={as_of_time}"
                    )
                compared += 1
    databases = operational.execute("PRAGMA database_list").fetchall()
    attached = [row["name"] for row in databases if row["name"] not in {"main", "temp"}]
    if attached:
        _fail(f"operational connection has attached databases: {attached}")
    return f"read_as_of passed availability and episode isolation across {compared} table snapshots"


def _primary_key(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    columns = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    primary = tuple(
        row["name"]
        for row in sorted(columns, key=lambda item: item["pk"])
        if row["pk"]
    )
    if not primary:
        _fail(f"as-of table {table} must have a primary key")
    return primary


def _validate_pending(private: sqlite3.Connection, metadata: dict[str, Any], operational: sqlite3.Connection) -> str:
    simulator_config = validate_simulator_config(metadata["resolved_config"]["simulator"])
    episode_id = next(
        row[0]
        for row in operational.execute(
            "SELECT episode_id FROM episodes WHERE episode_id <> 'base' ORDER BY episode_id LIMIT 1"
        )
    )
    snapshots = snapshot_labels(
        operational,
        simulator_config,
        episode_id=episode_id,
        as_of_time=T0_UTC_TEXT,
    )
    from model.data import target_for_label_state

    for snapshot in snapshots:
        target = target_for_label_state(snapshot.state)
        if snapshot.state == "pending" and (snapshot.label is not None or target is not None):
            _fail(f"pending transaction {snapshot.txn_id} was represented as a negative label")
        if snapshot.state == "matured_negative" and target != 0:
            _fail(f"matured-negative transaction {snapshot.txn_id} has the wrong target")
        if snapshot.state == "confirmed_fraud" and target != 1:
            _fail(f"confirmed-fraud transaction {snapshot.txn_id} has the wrong target")
        if snapshot.state == "confirmed_legit" and target != 0:
            _fail(f"confirmed-legit transaction {snapshot.txn_id} has the wrong target")
    return f"{len(snapshots)} label snapshots preserve pending as unlabeled under {LABEL_POLICY_VERSION}"


def _walk_keys(value: Any):
    if isinstance(value, dict):
        for key, nested in value.items():
            yield str(key)
            yield from _walk_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_keys(nested)


def _validate_reports(run_dir: Path) -> str:
    public_path = run_dir / "reports" / "smoke.json"
    private_path = run_dir / "private" / "reports" / "smoke_truth.json"
    if not public_path.is_file() or not private_path.is_file():
        _fail("smoke operational report or private truth report is missing")
    try:
        public_report = json.loads(public_path.read_text(encoding="utf-8"))
        private_report = json.loads(private_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _fail(f"smoke report is unreadable or invalid JSON: {exc}")
    if public_report.get("profile") != "smoke" or public_report.get("report_version") != 1:
        _fail("public smoke report has an unsupported version/profile")
    if not {
        "as_of_time",
        "label_policy_version",
        "model_version",
    } <= set(public_report):
        _fail("public smoke report is missing label-policy or model-version metadata")
    if not isinstance(public_report["label_policy_version"], str) or not public_report["label_policy_version"]:
        _fail("public smoke report label_policy_version must be non-empty")
    if public_report["model_version"] is not None and not isinstance(
        public_report["model_version"], str
    ):
        _fail("public smoke report model_version must be a string or null")
    forbidden = sorted({key for key in _walk_keys(public_report) if key in _PUBLIC_FORBIDDEN_KEYS})
    if forbidden:
        _fail(f"operational report contains private truth fields: {forbidden}")
    support = public_report.get("p2_cohort_support")
    required_cohorts = {"fit", "validation", "calibration", "reference_drift", "monitoring_test"}
    if not isinstance(support, dict) or not required_cohorts <= set(support):
        _fail("public smoke report is missing a P2 support cohort")
    for name in ("fit", "validation", "calibration"):
        entry = support[name]
        if not {"positive_count", "negative_count", "missing_support", "support_status"} <= set(entry):
            _fail(f"P2 {name} support report is incomplete")
    if "operational_distributions" not in public_report or "window_completeness" not in public_report:
        _fail("public smoke report is missing channel/time distribution or window completeness")
    if not isinstance(private_report, dict) or private_report.get("truth_bearing") is not True:
        _fail("private truth report is not marked truth_bearing")
    if "truth_label_support" not in private_report or "label_delay_oracle" not in private_report:
        _fail("private smoke report is missing truth support or oracle label-delay results")
    return "operational support/distribution/window report is public-safe; truth and oracle report is private"


def _validate_p2_handoff_support(run_dir: Path) -> str:
    report_path = run_dir / "reports" / "smoke.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    cohorts = report["p2_cohort_support"]
    missing: list[str] = []
    for split in ("fit", "validation", "calibration"):
        support = cohorts[split]
        if support["positive_count"] < 1:
            missing.append(f"{split}:positive")
        if support["negative_count"] < 1:
            missing.append(f"{split}:negative")
        if support["support_status"] != "sufficient":
            missing.append(f"{split}:status={support['support_status']}")
    reference = cohorts["reference_drift"]
    if reference["transaction_count"] < 1 or reference["support_status"] != "available":
        missing.append("reference_drift:missing_reference")
    if missing:
        _fail("required P2 handoff support is missing: " + ", ".join(missing))

    calibration = cohorts["calibration"]
    if calibration["calibration_minimum_met"] is False and not any(
        "fallback_method=sigmoid" in warning for warning in calibration.get("warnings", [])
    ):
        _fail("calibration isotonic shortfall must be reported as a sigmoid fallback warning")
    return (
        "fit, validation, calibration each contain positive and negative observed labels; "
        "reference cohort is present; isotonic shortfall is a non-blocking fallback warning"
    )


def _validate_private_label_truth(
    operational: sqlite3.Connection,
    private: sqlite3.Connection,
) -> str:
    all_outcomes: dict[str, dict[str, Any]] = {}
    metadata_by_episode: dict[str, dict[str, Any]] = {}
    rows = private.execute(
        "SELECT key, value_json FROM run_metadata WHERE key LIKE 'p1.5.label_truth.v1%'"
    ).fetchall()
    if not rows:
        _fail("private P1.5 label truth metadata is missing")
    for metadata_row in rows:
        metadata = json.loads(metadata_row["value_json"])
        episode_id = metadata.get("episode_id")
        if not isinstance(episode_id, str) or not episode_id:
            _fail("private label truth must identify its episode")
        if episode_id in metadata_by_episode:
            _fail(f"private label truth duplicates episode {episode_id!r}")
        metadata_by_episode[episode_id] = metadata
        for outcome in metadata.get("transactions", []):
            txn_id = outcome.get("txn_id")
            if txn_id in all_outcomes:
                _fail(f"private label truth duplicates transaction {txn_id!r}")
            all_outcomes[txn_id] = outcome
    transactions = {
        row["txn_id"]: dict(row) for row in operational.execute("SELECT * FROM transactions")
    }
    if set(transactions) != set(all_outcomes):
        _fail(
            "private label truth transaction coverage differs "
            f"(missing={len(set(transactions) - set(all_outcomes))}, "
            f"extra={len(set(all_outcomes) - set(transactions))})"
        )
    for metadata_row in private.execute(
        "SELECT key, value_json FROM run_metadata "
        "WHERE substr(key, 1, ?) = ?",
        (len("p1.6.fraud_groups.v1:"), "p1.6.fraud_groups.v1:"),
    ):
        try:
            p16_metadata = json.loads(metadata_row["value_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            _fail(f"P1.6 fraud label metadata is invalid JSON: {exc}")
        for group in p16_metadata.get("groups", []) if isinstance(p16_metadata, dict) else []:
            transaction_ids = group.get("transaction_ids", []) if isinstance(group, dict) else []
            if any(
                txn_id not in all_outcomes or all_outcomes[txn_id].get("true_label") != "fraud"
                for txn_id in transaction_ids
            ):
                _fail("P1.6 generated fraud group does not have fraud labels in private P1.5 truth")
    labels = {
        row["txn_id"]: dict(row) for row in operational.execute("SELECT * FROM label_events")
    }
    for txn_id, outcome in all_outcomes.items():
        transaction = transactions[txn_id]
        metadata = metadata_by_episode.get(transaction["episode_id"])
        if metadata is None:
            _fail(f"private label policy is missing for transaction {txn_id}")
        if outcome.get("true_label") not in {"fraud", "legit"}:
            _fail(f"private label truth has invalid target for {txn_id}")
        if outcome.get("event_at") != transaction["event_at"]:
            _fail(f"private label truth event_at differs for {txn_id}")
        random_key = outcome.get("random_key")
        seed = metadata.get("seed")
        probability_reported = metadata.get("probability_reported")
        minimum_delay = metadata.get("minimum_delay_days")
        maximum_delay = metadata.get("maximum_delay_days")
        if (
            not isinstance(random_key, str)
            or not random_key
            or type(seed) is not int
            or type(probability_reported) not in {int, float}
            or type(minimum_delay) is not int
            or type(maximum_delay) is not int
        ):
            _fail(f"private label truth has invalid stable draw inputs for {txn_id}")
        expected_reported = (
            keyed_rng(seed, "p1.labels.reporting", random_key).random()
            < probability_reported
        )
        if outcome.get("reported") is not expected_reported:
            _fail(f"private label report draw does not reproduce for {txn_id}")
        expected_delay = (
            keyed_rng(seed, "p1.labels.delay_days", random_key).randint(
                minimum_delay, maximum_delay
            )
            if expected_reported
            else None
        )
        if outcome.get("report_delay_days") != expected_delay:
            _fail(f"private label delay draw does not reproduce for {txn_id}")
        label_event = labels.get(txn_id)
        if expected_reported != (label_event is not None):
            _fail(f"operational reported-label presence differs from private outcome for {txn_id}")
        if label_event is not None:
            if label_event["label"] != outcome["true_label"]:
                _fail(f"operational observed label differs from private outcome for {txn_id}")
            expected_reported_at = utc_timestamp(
                datetime.fromisoformat(transaction["event_at"].replace("Z", "+00:00"))
                + timedelta(days=expected_delay)
            )
            if label_event["reported_at"] != expected_reported_at:
                _fail(f"operational reported_at differs from the stable label draw for {txn_id}")
    return (
        f"private label truth covers {len(transactions)} transactions; report/delay draws "
        "reproduce from private stable keys and observed labels agree"
    )


def _evaluate(checks: list[dict[str, Any]], name: str, check: Callable[[], str]) -> None:
    try:
        detail = check()
    except Exception as exc:  # each finding is captured so one error cannot hide later violations
        checks.append({"name": name, "passed": False, "detail": f"{type(exc).__name__}: {exc}"})
    else:
        checks.append({"name": name, "passed": True, "detail": detail})


def _record_validation(private: sqlite3.Connection, metadata: dict[str, Any] | None, result: dict[str, Any]) -> None:
    if not private.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_metadata'"
    ).fetchone():
        return
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if private.execute(
        "SELECT 1 FROM run_metadata WHERE key = ?", (VALIDATION_METADATA_KEY,)
    ).fetchone():
        private.execute(
            "UPDATE run_metadata SET value_json = ? WHERE key = ?",
            (encoded, VALIDATION_METADATA_KEY),
        )
    else:
        private.execute(
            "INSERT INTO run_metadata (key, value_json) VALUES (?, ?)",
            (VALIDATION_METADATA_KEY, encoded),
        )
    if metadata is not None:
        metadata["validation"] = result
        private.execute(
            "UPDATE run_metadata SET value_json = ? WHERE key = ?",
            (
                json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                RUN_METADATA_KEY,
            ),
        )
    private.commit()


def _validate_p14_mechanism_chains(
    operational: sqlite3.Connection, private: sqlite3.Connection
) -> str:
    """Check P1.4 private group references against actual transaction chains."""
    metadata_row = private.execute(
        "SELECT value_json FROM run_metadata WHERE key = ?", (RUN_METADATA_KEY,)
    ).fetchone()
    if metadata_row is None:
        _fail("P1.8 run metadata is required to validate P1.4 mechanisms")
    run_metadata = json.loads(metadata_row[0])
    simulator_config = run_metadata.get("resolved_config", {}).get("simulator", {})
    timeline = simulator_config.get("timeline", {})
    drift_value = timeline.get("drift_start_business_date", {}).get("value")
    fraud_config = simulator_config.get("fraud", {})
    near_min = fraud_config.get("near_threshold_minimum_amount", {}).get("value")
    near_max = fraud_config.get("near_threshold_maximum_amount", {}).get("value")
    social_config = fraud_config.get("social_engineering", {})
    control_min = social_config.get("business_control_minimum_amount", {}).get("value")
    control_max = social_config.get("business_control_maximum_amount", {}).get("value")
    new_account_warmup_days = social_config.get("new_account_warmup_days", {}).get("value")
    mule_window = simulator_config.get("world", {}).get(
        "internal_mule_outbound_window_hours", {}
    ).get("value")
    if (
        not isinstance(drift_value, str)
        or type(mule_window) is not int
        or type(near_min) is not int
        or type(near_max) is not int
        or type(control_min) is not int
        or type(control_max) is not int
        or type(new_account_warmup_days) is not int
    ):
        _fail("resolved simulator configuration is missing P1.4 timing or amount contracts")
    drift_start = datetime.combine(
        datetime.fromisoformat(drift_value).date(), time.min,
        tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"),
    ).astimezone(UTC)
    transactions = {
        row["txn_id"]: dict(row) for row in operational.execute("SELECT * FROM transactions")
    }
    pre_injection_amounts, _, _ = _pipeline_effect_amounts(private)
    events_by_txn: dict[str, list[dict[str, Any]]] = {}
    for row in operational.execute("SELECT * FROM transaction_events"):
        events_by_txn.setdefault(row["txn_id"], []).append(dict(row))
    checked_groups = 0
    episode_ids = [row[0] for row in operational.execute("SELECT episode_id FROM episodes")]
    templates = {
        row["episode_id"]: row["template"]
        for row in private.execute("SELECT episode_id, template FROM episode_manifest")
    }
    for episode_id in episode_ids:
        key = "p1.4.mechanisms.v1" if episode_id == "base" else f"p1.4.mechanisms.v1:{episode_id}"
        metadata_row = private.execute(
            "SELECT value_json FROM run_metadata WHERE key = ?", (key,)
        ).fetchone()
        if metadata_row is None:
            _fail(f"P1.4 mechanism metadata is missing for {episode_id}")
        metadata = json.loads(metadata_row[0])
        groups = metadata.get("groups") if isinstance(metadata, dict) else None
        if metadata.get("episode_id") != episode_id or not isinstance(groups, list):
            _fail(f"P1.4 mechanism metadata is malformed for {episode_id}")
        if templates.get(episode_id) == "clean" and groups:
            _fail(f"clean episode {episode_id} contains P1.4 mechanism groups")
        for group in groups:
            if not isinstance(group, dict) or group.get("role") not in {"fraud", "benign_control"}:
                _fail(f"P1.4 mechanism group has no private role for {episode_id}")
            mechanism = group.get("mechanism")
            if mechanism not in {
                "ato",
                "collusion",
                "social_engineering",
                "legit_business_spike",
                "household_business_activity",
            }:
                _fail(f"P1.4 mechanism group has an unsupported type for {episode_id}")
            business_mechanisms = {"legit_business_spike", "household_business_activity"}
            expected_role = "benign_control" if mechanism in business_mechanisms else "fraud"
            if group["role"] != expected_role:
                _fail(f"P1.4 {mechanism} group has an inconsistent private role")
            if mechanism == "collusion" and episode_id != "base":
                _fail("P1.4 collusion must remain in shared pre-drift history")
            if mechanism in {"social_engineering", "legit_business_spike"} and episode_id == "base":
                _fail(f"P1.4 {mechanism} must be generated only by its P1.6 template injector")
            if mechanism == "household_business_activity" and episode_id != "base":
                _fail("P1.4 ordinary household-business activity must remain in base history")
            txn_ids = [
                txn_id
                for field in ("transaction_ids", "incoming_transaction_ids", "outgoing_transaction_ids")
                for txn_id in group.get(field, [])
            ]
            if not txn_ids:
                _fail(f"P1.4 {mechanism} group contains no transactions")
            group_rows: dict[str, dict[str, Any]] = {}
            for txn_id in txn_ids:
                transaction = transactions.get(txn_id)
                if transaction is None or transaction["episode_id"] != episode_id:
                    _fail(f"P1.4 {mechanism} group references a missing or cross-episode transaction")
                group_rows[txn_id] = transaction
                if mechanism == "collusion" and datetime.fromisoformat(
                    transaction["event_at"].replace("Z", "+00:00")
                ).astimezone(UTC) >= drift_start:
                    _fail("P1.4 collusion transaction is on or after the drift point")
                amount = pre_injection_amounts.get(txn_id, transaction["amount"])
                if mechanism in business_mechanisms:
                    amount_in_range = type(amount) is int and control_min <= amount <= control_max
                else:
                    amount_in_range = type(amount) is int and near_min <= amount <= near_max
                if not amount_in_range:
                    _fail(f"P1.4 {mechanism} transaction amount is outside its configured band")
            if mechanism == "ato":
                expected_amounts = group.get("amounts")
                transaction_ids = group.get("transaction_ids")
                if (
                    not isinstance(expected_amounts, list)
                    or not isinstance(transaction_ids, list)
                    or len(expected_amounts) != len(transaction_ids)
                    or any(
                        pre_injection_amounts.get(txn_id, group_rows[txn_id]["amount"]) != amount
                        for txn_id, amount in zip(transaction_ids, expected_amounts, strict=True)
                    )
                ):
                    _fail("P1.4 ATO transactions do not match private amount metadata")
                if group.get("device_signature") is True:
                    ato_rows = [group_rows[item] for item in transaction_ids]
                    device_ids = {row["device_id"] for row in ato_rows}
                    customer_ids = {row["customer_id"] for row in ato_rows}
                    first_created_at = min(row["created_at"] for row in ato_rows)
                    if len(device_ids) != 1 or None in device_ids or len(customer_ids) != 1:
                        _fail("P1.4 signed ATO group must use one new device for one customer")
                    customer_id = next(iter(customer_ids))
                    device_id = next(iter(device_ids))
                    prior_device = operational.execute(
                        """
                        SELECT 1 FROM transactions
                        WHERE customer_id = ? AND device_id = ?
                          AND created_at < ? AND available_at <= ?
                          AND (
                            episode_id = ?
                            OR (episode_id = 'base' AND ? <> 'base' AND event_at < ?)
                          )
                        LIMIT 1
                        """,
                        (
                            customer_id,
                            device_id,
                            first_created_at,
                            first_created_at,
                            episode_id,
                            episode_id,
                            T0_UTC_TEXT,
                        ),
                    ).fetchone()
                    if prior_device is not None:
                        _fail(
                            "P1.4 signed ATO device was already available for the customer "
                            "before the campaign"
                        )
            if mechanism in {"collusion", "social_engineering"}:
                incoming_ids = group.get("incoming_transaction_ids", [])
                outgoing_ids = group.get("outgoing_transaction_ids", [])
                amounts = group.get("amounts", [])
                mule_id = group.get("mule_customer_id")
                if not (
                    isinstance(incoming_ids, list) and isinstance(outgoing_ids, list)
                    and isinstance(amounts, list)
                    and len(incoming_ids) == len(outgoing_ids) == len(amounts) > 0
                    and isinstance(mule_id, str)
                ):
                    _fail(f"P1.4 {mechanism} cash-out chain is malformed")
                expected_senders = group.get(
                    "member_customer_ids" if mechanism == "collusion" else "victim_customer_ids"
                )
                actual_senders = [group_rows[item]["customer_id"] for item in incoming_ids]
                if (
                    not isinstance(expected_senders, list)
                    or len(set(actual_senders)) != len(actual_senders)
                    or set(actual_senders) != set(expected_senders)
                ):
                    _fail(f"P1.4 {mechanism} sender membership does not match its transaction chain")
                for incoming_id, outgoing_id, amount in zip(incoming_ids, outgoing_ids, amounts, strict=True):
                    incoming = group_rows[incoming_id]
                    outgoing = group_rows[outgoing_id]
                    if (
                        incoming["recipient_customer_id"] != mule_id
                        or outgoing["customer_id"] != mule_id
                        or outgoing["recipient_customer_id"] != group.get("cashout_customer_id")
                        or pre_injection_amounts.get(incoming_id, incoming["amount"]) != amount
                        or pre_injection_amounts.get(outgoing_id, outgoing["amount"]) != amount
                        or incoming["created_at"] >= outgoing["created_at"]
                    ):
                        _fail(f"P1.4 {mechanism} cash-out does not preserve incoming funds")
                    settled_at = [
                        event["event_at"] for event in events_by_txn.get(incoming_id, [])
                        if event["event_type"] == "settled"
                    ]
                    if not settled_at or min(settled_at) >= outgoing["created_at"]:
                        _fail(f"P1.4 {mechanism} cash-out precedes incoming settlement")
                    delay = datetime.fromisoformat(outgoing["created_at"].replace("Z", "+00:00")) - datetime.fromisoformat(incoming["created_at"].replace("Z", "+00:00"))
                    if delay > timedelta(hours=mule_window):
                        _fail(f"P1.4 {mechanism} cash-out exceeds the mule capability window")
                if mechanism == "social_engineering":
                    if group.get("mule_role") != "internal_mule":
                        _fail("P1.4 social-engineering recipient must have the private internal-mule role")
                    mule = operational.execute(
                        "SELECT event_at FROM customers WHERE customer_id = ?", (mule_id,)
                    ).fetchone()
                    first_receipt = datetime.fromisoformat(
                        min(group_rows[item]["created_at"] for item in incoming_ids).replace(
                            "Z", "+00:00"
                        )
                    ).astimezone(UTC)
                    mule_available_at = (
                        datetime.fromisoformat(mule["event_at"].replace("Z", "+00:00"))
                        .astimezone(UTC)
                        if mule is not None else None
                    )
                    if (
                        mule_available_at is None
                        or first_receipt - mule_available_at
                        < timedelta(days=new_account_warmup_days)
                    ):
                        _fail(
                            "P1.4 social-engineering mule lacks the configured as-of history "
                            "before receipt"
                        )
            if mechanism in business_mechanisms:
                recipient_id = group.get("recipient_customer_id")
                control_rows = [group_rows[item] for item in group.get("transaction_ids", [])]
                expected_senders = group.get("sender_customer_ids")
                expected_amounts = group.get("amounts")
                actual_senders = {item["customer_id"] for item in control_rows}
                if (
                    not isinstance(recipient_id, str)
                    or not isinstance(expected_senders, list)
                    or len(actual_senders) < 2
                    or actual_senders != set(expected_senders)
                    or group.get("fan_in_count") != len(actual_senders)
                    or not isinstance(expected_amounts, list)
                    or len(expected_amounts) != len(control_rows)
                    or any(
                        pre_injection_amounts.get(txn_id, row["amount"]) != amount
                        for txn_id, row, amount in zip(
                            group.get("transaction_ids", []), control_rows, expected_amounts,
                            strict=True,
                        )
                    )
                    or any(item["recipient_customer_id"] != recipient_id for item in control_rows)
                ):
                    _fail("P1.4 business control lacks its high-fan-in transfers")
            checked_groups += 1
    return f"{checked_groups} P1.4 mechanism groups match private membership, episode, amounts, and timing"


def _validate_operational_identifier_shapes(
    operational: sqlite3.Connection, private: sqlite3.Connection
) -> str:
    """Ensure IDs have one observable shape across truth classes and streams."""
    transaction_pattern = re.compile(r"txn-[0-9a-f]{64}\Z")
    customer_pattern = re.compile(r"customer-[0-9]{6}\Z")
    transaction_ids = [
        row[0] for row in operational.execute("SELECT txn_id FROM transactions")
    ]
    customer_ids = [
        row[0] for row in operational.execute("SELECT customer_id FROM customers")
    ]
    if not transaction_ids or not customer_ids:
        _fail("generated transaction and customer IDs must be present")
    if any(not isinstance(item, str) or not transaction_pattern.fullmatch(item) for item in transaction_ids):
        _fail("operational transaction IDs must all use the uniform txn- plus SHA-256 shape")
    if any(not isinstance(item, str) or not customer_pattern.fullmatch(item) for item in customer_ids):
        _fail("operational customer IDs must all use the uniform six-digit customer shape")

    fraud_shapes: set[str] = set()
    legit_shapes: set[str] = set()
    for episode_id, in operational.execute("SELECT episode_id FROM episodes"):
        key = (
            "p1.5.label_truth.v1"
            if episode_id == "base"
            else f"p1.5.label_truth.v1:{episode_id}"
        )
        truth_row = private.execute(
            "SELECT value_json FROM run_metadata WHERE key = ?", (key,)
        ).fetchone()
        if truth_row is None:
            _fail(f"private label truth is missing for identifier-shape audit: {episode_id}")
        outcomes = json.loads(truth_row[0]).get("transactions", [])
        for outcome in outcomes:
            shape = "sha256" if transaction_pattern.fullmatch(outcome["txn_id"]) else "other"
            if outcome["true_label"] == "fraud":
                fraud_shapes.add(shape)
            elif outcome["true_label"] == "legit":
                legit_shapes.add(shape)
    if fraud_shapes and legit_shapes and fraud_shapes != legit_shapes:
        _fail("transaction ID format class differs between private fraud and legitimate truth")
    return (
        f"{len(transaction_ids)} transactions share one SHA-256 ID shape; "
        f"{len(customer_ids)} customers share one six-digit ID shape; "
        "private fraud and legitimate labels see identical transaction format classes"
    )


def _validate_legitimate_new_account_control(
    operational: sqlite3.Connection, private: sqlite3.Connection
) -> str:
    metadata_row = private.execute(
        "SELECT value_json FROM run_metadata WHERE key = ?",
        ("p1.4.legitimate_new_accounts.v1",),
    ).fetchone()
    if metadata_row is None:
        _fail("private legitimate-new-account control metadata is missing")
    cohort = json.loads(metadata_row[0])
    if not isinstance(cohort, dict) or cohort.get("episode_id") != "base":
        _fail("legitimate-new-account control must be recorded in base-history metadata")
    customer_ids = cohort.get("customer_ids")
    if (
        not isinstance(customer_ids, list)
        or any(not isinstance(item, str) for item in customer_ids)
        or len(customer_ids) != len(set(customer_ids))
        or cohort.get("count") != len(customer_ids)
    ):
        _fail("private legitimate-new-account control membership is malformed")
    run_row = private.execute(
        "SELECT value_json FROM run_metadata WHERE key = ?", (RUN_METADATA_KEY,)
    ).fetchone()
    if run_row is None:
        _fail("P1.8 run metadata is required for legitimate-new-account validation")
    run_metadata = json.loads(run_row[0])
    expected_count = (
        run_metadata.get("resolved_config", {})
        .get("simulator", {})
        .get("world", {})
        .get("legitimate_new_accounts_per_base_history", {})
        .get("value")
    )
    if type(expected_count) is not int or len(customer_ids) != expected_count:
        _fail("legitimate-new-account cohort size differs from resolved simulator config")
    if not customer_ids:
        return "configuration requests no legitimate new-account controls"

    placeholders = ", ".join("?" for _ in customer_ids)
    customers = {
        row["customer_id"]: dict(row)
        for row in operational.execute(
            "SELECT customer_id, episode_id, event_at FROM customers "
            f"WHERE customer_id IN ({placeholders})",
            tuple(customer_ids),
        )
    }
    if set(customers) != set(customer_ids) or any(
        row["episode_id"] != "base" for row in customers.values()
    ):
        _fail("legitimate new-account controls must be registered in base history")
    history_start = operational.execute(
        "SELECT MIN(event_at) FROM customers WHERE episode_id = 'base'"
    ).fetchone()[0]
    if any(row["event_at"] <= history_start for row in customers.values()):
        _fail("legitimate new-account controls must be created after base history starts")

    mule_ids: set[str] = set()
    for row in private.execute(
        "SELECT value_json FROM run_metadata WHERE key LIKE 'p1.4.mechanisms.v1%'"
    ):
        value = json.loads(row[0])
        for group in value.get("groups", []):
            mule_id = group.get("mule_customer_id")
            if isinstance(mule_id, str):
                mule_ids.add(mule_id)
    if set(customer_ids) & mule_ids:
        _fail("legitimate new-account controls overlap private P1.4 mule participants")

    transactions = [
        dict(row)
        for row in operational.execute(
            "SELECT txn_id, episode_id, customer_id, merchant_id, recipient_customer_id "
            f"FROM transactions WHERE customer_id IN ({placeholders})",
            tuple(customer_ids),
        )
    ]
    by_customer: dict[str, list[dict[str, Any]]] = {item: [] for item in customer_ids}
    for transaction in transactions:
        by_customer[transaction["customer_id"]].append(transaction)
        if (
            transaction["episode_id"] != "base"
            or transaction["merchant_id"] is None
            or transaction["recipient_customer_id"] is not None
        ):
            _fail("legitimate new-account controls must have ordinary base merchant payments")
    if any(not values for values in by_customer.values()):
        _fail("each legitimate new-account control must have merchant payment history")

    label_truth_row = private.execute(
        "SELECT value_json FROM run_metadata WHERE key = 'p1.5.label_truth.v1'"
    ).fetchone()
    if label_truth_row is None:
        _fail("base private label truth is required for new-account controls")
    outcomes = {
        item.get("txn_id"): item
        for item in json.loads(label_truth_row[0]).get("transactions", [])
    }
    if any(
        outcomes.get(transaction["txn_id"], {}).get("true_label") != "legit"
        for transaction in transactions
    ):
        _fail("legitimate new-account merchant payments must have private legit truth")
    return (
        f"{len(customer_ids)} legitimate new accounts occur after base-history start, "
        "have ordinary merchant history, and are separate from mule truth"
    )


def validate_run(run_dir: str | Path) -> dict[str, Any]:
    """Validate one generated run and record the result in its private metadata."""
    root = Path(run_dir).expanduser().resolve()
    operational_path = root / "operational" / "observations.sqlite"
    private_path = root / "private" / "evaluator.sqlite"
    checks: list[dict[str, Any]] = []
    operational: sqlite3.Connection | None = None
    private: sqlite3.Connection | None = None
    metadata: dict[str, Any] | None = None

    def check_files() -> str:
        missing = [str(path.relative_to(root)) for path in (operational_path, private_path) if not path.is_file()]
        if missing:
            _fail(f"missing run database files: {missing}")
        if operational_path.resolve() == private_path.resolve():
            _fail("operational and private database files must be physically separate")
        if operational_path.parent.name != "operational" or private_path.parent.name != "private":
            _fail("operational and private databases must remain in their separate directories")
        return "operational and private SQLite files are present at separate paths"

    _evaluate(checks, "run_files_and_truth_separation", check_files)
    if operational_path.is_file():
        try:
            operational = _open_readonly(operational_path)
        except sqlite3.Error as exc:
            checks.append({"name": "open_operational", "passed": False, "detail": str(exc)})
    if private_path.is_file():
        try:
            private = _open_private(private_path)
        except sqlite3.Error as exc:
            checks.append({"name": "open_private", "passed": False, "detail": str(exc)})

    if operational is not None:
        _evaluate(checks, "operational_schema", lambda: _check_schema(operational, operational=True))
        _evaluate(checks, "operational_integrity_and_foreign_keys", lambda: _integrity_check(operational, "operational"))
        _evaluate(
            checks,
            "as_of_episode_contract",
            lambda: _collect_contract_violations(operational, private),
        )
        _evaluate(checks, "monitoring_output_contract", lambda: _validate_monitoring_outputs(operational))
        _evaluate(checks, "decision_output_contract", lambda: _validate_decisions(operational))
        _evaluate(checks, "as_of_reader_selection", lambda: _validate_asof(operational))
        if private is not None:
            _evaluate(checks, "episode_manifest_and_fingerprints", lambda: _validate_episode_manifest(operational, private, _load_run_metadata(private)))
            _evaluate(
                checks,
                "transaction_event_episode_window",
                lambda: _validate_transaction_event_episode_window(
                    operational, _load_run_metadata(private)
                ),
            )
            _evaluate(checks, "incident_truth_contract", lambda: _validate_incident_truth(operational, private, _load_run_metadata(private)))
            _evaluate(checks, "operational_identifier_shape_contract", lambda: _validate_operational_identifier_shapes(operational, private))
            _evaluate(checks, "legitimate_new_account_control", lambda: _validate_legitimate_new_account_control(operational, private))
            _evaluate(checks, "p1_4_fraud_mechanism_chains", lambda: _validate_p14_mechanism_chains(operational, private))
            _evaluate(checks, "private_label_truth_matches_observed_labels", lambda: _validate_private_label_truth(operational, private))
            _evaluate(checks, "pending_is_not_negative", lambda: _validate_pending(private, _load_run_metadata(private), operational))
            _evaluate(checks, "run_metadata_and_canonical_fingerprint", lambda: _validate_metadata(operational, private, _load_run_metadata(private)))
        else:
            checks.append({"name": "private_contract", "passed": False, "detail": "private database could not be opened"})
    else:
        checks.append({"name": "operational_contract", "passed": False, "detail": "operational database could not be opened"})

    if private is not None:
        _evaluate(checks, "private_schema", lambda: _check_schema(private, operational=False))
        _evaluate(checks, "private_integrity_and_foreign_keys", lambda: _integrity_check(private, "private"))
        try:
            metadata = _load_run_metadata(private)
        except Exception:
            metadata = None
        _evaluate(checks, "report_truth_boundary", lambda: _validate_reports(root))
        _evaluate(checks, "p2_handoff_support", lambda: _validate_p2_handoff_support(root))
    else:
        checks.append({"name": "private_schema", "passed": False, "detail": "private database could not be opened"})

    passed = sum(bool(check["passed"]) for check in checks)
    failed = len(checks) - passed
    result = {
        "status": "passed" if failed == 0 else "failed",
        "checked_at": utc_timestamp(datetime.now(UTC)),
        "passed_checks": passed,
        "failed_checks": failed,
        "checks": checks,
    }
    if private is not None:
        try:
            _record_validation(private, metadata, result)
        except Exception as exc:
            result["status"] = "failed"
            result["failed_checks"] += 1
            result["checks"].append(
                {"name": "record_validation_result", "passed": False, "detail": f"{type(exc).__name__}: {exc}"}
            )
    if operational is not None:
        operational.close()
    if private is not None:
        private.close()
    return result

