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
    }
)
_MONITORING_COUNT_FLAG = re.compile(
    r"^(?:window_censored_samples|reference_censored_samples|"
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
            "accepted_actions": {"data_ticket", "human_review"},
        },
        "seasonal": {
            "cause": "seasonal",
            "requires_intervention": 0,
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
        expected = expected_contracts.get(template)
        if expected is None:
            _fail(f"unsupported round-one incident template in manifest: {template!r}")
        if len(truth_rows) != 1:
            _fail(
                f"episode {episode_id} template {template!r} requires exactly one incident truth; "
                f"found {len(truth_rows)}"
            )
        truth = truth_rows[0]
        effect_manifest = effect_manifests[truth["incident_truth_id"]]
        if effect_manifest["episode_id"] != episode_id:
            _fail(
                f"incident effect reference episode does not match incident truth for {episode_id}"
            )
        affected_ids = effect_manifest["affected_transaction_ids"]
        affected_rows: list[dict[str, Any]] = []
        for offset in range(0, len(affected_ids), 500):
            batch = affected_ids[offset : offset + 500]
            placeholders = ", ".join("?" for _ in batch)
            affected_rows.extend(
                dict(row)
                for row in operational.execute(
                    "SELECT txn_id, episode_id, event_at, available_at FROM transactions "
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
        if truth["cause"] != expected["cause"]:
            _fail(f"incident truth cause does not match {template!r} for {episode_id}")
        if truth["requires_intervention"] != expected["requires_intervention"]:
            _fail(f"incident truth requires_intervention does not match {template!r} for {episode_id}")
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
        if truth["parent_scenario"] is not None:
            _fail(f"round-one smoke truth must not have a parent scenario for {episode_id}")
        verified_incidents += 1
    return f"{verified_incidents} incident truth rows match locked templates and docs/data.md section 5"


def _collect_contract_violations(operational: sqlite3.Connection) -> str:
    violations: list[str] = []
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
        if row["created_at"] != row["event_at"]:
            violations.append(f"transaction {txn_id} has created_at != event_at")
        event_at = row["event_at"]
        episode_id = row["episode_id"]
        if (episode_id == "base" and event_at >= T0_UTC_TEXT) or (
            episode_id != "base" and event_at < T0_UTC_TEXT
        ):
            violations.append(f"transaction {txn_id} is on the wrong side of T0 for {episode_id}")
        customer_episode = entity_episodes["customers"].get(row["customer_id"])
        merchant_episode = entity_episodes["merchants"].get(row["merchant_id"])
        if customer_episode not in {"base", episode_id}:
            violations.append(f"transaction {txn_id} references a customer from another episode")
        if merchant_episode not in {"base", episode_id}:
            violations.append(f"transaction {txn_id} references a merchant from another episode")

    for row in operational.execute("SELECT * FROM transaction_events"):
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
    rows = private.execute(
        "SELECT key, value_json FROM run_metadata WHERE key LIKE 'p1.5.label_truth.v1%'"
    ).fetchall()
    if not rows:
        _fail("private P1.5 label truth metadata is missing")
    for metadata_row in rows:
        metadata = json.loads(metadata_row["value_json"])
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
    labels = {
        row["txn_id"]: dict(row) for row in operational.execute("SELECT * FROM label_events")
    }
    for txn_id, outcome in all_outcomes.items():
        transaction = transactions[txn_id]
        if outcome.get("true_label") not in {"fraud", "legit"}:
            _fail(f"private label truth has invalid target for {txn_id}")
        if outcome.get("event_at") != transaction["event_at"]:
            _fail(f"private label truth event_at differs for {txn_id}")
        label_event = labels.get(txn_id)
        if bool(outcome.get("reported")) != (label_event is not None):
            _fail(f"operational reported-label presence differs from private outcome for {txn_id}")
        if label_event is not None and label_event["label"] != outcome["true_label"]:
            _fail(f"operational observed label differs from private outcome for {txn_id}")
    return f"private label truth covers {len(transactions)} transactions; reported labels agree"


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
        _evaluate(checks, "as_of_episode_contract", lambda: _collect_contract_violations(operational))
        _evaluate(checks, "monitoring_output_contract", lambda: _validate_monitoring_outputs(operational))
        _evaluate(checks, "decision_output_contract", lambda: _validate_decisions(operational))
        _evaluate(checks, "as_of_reader_selection", lambda: _validate_asof(operational))
        if private is not None:
            _evaluate(checks, "episode_manifest_and_fingerprints", lambda: _validate_episode_manifest(operational, private, _load_run_metadata(private)))
            _evaluate(checks, "incident_truth_contract", lambda: _validate_incident_truth(operational, private, _load_run_metadata(private)))
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

