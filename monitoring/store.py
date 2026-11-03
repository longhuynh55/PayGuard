"""Idempotent persistence for truth-free P3 monitoring snapshots."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any

from simulator.common import primary_key_columns, table_columns
from simulator.schema import OPERATIONAL_TIMESTAMP_COLUMNS
from simulator.storage import StorageError, insert_operational_record, utc_timestamp


MONITORING_TABLES = frozenset({"daily_metrics", "alerts", "incidents"})
_JSON_COLUMNS = frozenset({"flags", "alert_ids_json"})


def _normalize_record(table: str, record: Mapping[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for column, value in record.items():
        if column in OPERATIONAL_TIMESTAMP_COLUMNS[table] and value is not None:
            value = utc_timestamp(value)
        elif column in _JSON_COLUMNS:
            if isinstance(value, str):
                try:
                    json.loads(value)
                except json.JSONDecodeError as exc:
                    raise StorageError(f"{column} must contain valid JSON") from exc
            else:
                value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        normalized[column] = value
    return normalized


def persist_monitoring_rows(
    connection: sqlite3.Connection,
    table: str,
    rows: Iterable[Mapping[str, Any]],
) -> int:
    """Insert monitoring rows once, accepting only identical repeated snapshots.

    Monitoring tables are append-only snapshots. Replaying a job with the same
    inputs is a no-op; a row that reuses a snapshot key with changed content is
    rejected rather than rewriting history.
    """
    if table not in MONITORING_TABLES:
        raise StorageError(f"not a monitoring output table: {table!r}")
    columns = table_columns(connection, table)
    primary_key = primary_key_columns(connection, table)
    if not columns or not primary_key:
        raise StorageError(f"monitoring output table is missing from this database: {table!r}")

    inserted = 0
    connection.execute("SAVEPOINT persist_monitoring_rows")
    try:
        for index, source in enumerate(rows):
            if not isinstance(source, Mapping):
                raise StorageError(f"{table} row {index} must be a mapping")
            record = dict(source)
            if set(record) != set(columns):
                raise StorageError(f"{table} row {index} does not match the operational schema")
            normalized = _normalize_record(table, record)
            where = " AND ".join(f'"{column}" = ?' for column in primary_key)
            key_values = tuple(normalized[column] for column in primary_key)
            existing = connection.execute(
                f'SELECT * FROM "{table}" WHERE {where}', key_values
            ).fetchone()
            if existing is not None:
                if any(existing[column] != normalized[column] for column in columns):
                    raise StorageError(
                        f"{table} snapshot key already exists with different content"
                    )
                continue
            insert_operational_record(connection, table, record)
            inserted += 1
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT persist_monitoring_rows")
        connection.execute("RELEASE SAVEPOINT persist_monitoring_rows")
        raise
    connection.execute("RELEASE SAVEPOINT persist_monitoring_rows")
    return inserted
