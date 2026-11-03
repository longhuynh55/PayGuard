"""Create and write the two physically separate P1 SQLite exports."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from simulator.common import (
    TimestampParseError,
    format_timestamp,
    parse_timestamp,
    table_columns,
)
from simulator.schema import (
    OPERATIONAL_DDL,
    OPERATIONAL_TABLES,
    OPERATIONAL_TIMESTAMP_COLUMNS,
    PRIVATE_DDL,
    PRIVATE_TABLES,
    PRIVATE_TIMESTAMP_COLUMNS,
)


class StorageError(ValueError):
    """Raised when an export path or record violates the storage contract."""


def utc_timestamp(value: datetime | str) -> str:
    """Return a timezone-aware timestamp as fixed-width UTC text."""
    if isinstance(value, str):
        try:
            return format_timestamp(parse_timestamp(value))
        except TimestampParseError as exc:
            if exc.reason == "invalid":
                raise StorageError(f"invalid ISO timestamp: {value!r}") from exc
            raise StorageError(
                "naive timestamps are not allowed; provide an explicit UTC offset"
            ) from exc
    if not isinstance(value, datetime):
        raise StorageError("timestamp must be an aware datetime or ISO string with an offset")
    if value.tzinfo is None or value.utcoffset() is None:
        raise StorageError("naive timestamps are not allowed; provide an explicit UTC offset")
    return format_timestamp(value)


@dataclass
class StorePair:
    """Open connections to independent operational and private files."""

    operational_path: Path
    private_path: Path
    operational: sqlite3.Connection
    private: sqlite3.Connection

    def commit(self) -> None:
        self.operational.commit()
        self.private.commit()

    def rollback(self) -> None:
        self.operational.rollback()
        self.private.rollback()

    def close(self) -> None:
        self.operational.close()
        self.private.close()

    def __enter__(self) -> StorePair:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        self.close()


class _StorageConnection(sqlite3.Connection):
    """Connection opened by this module; only these may memoize per-table column lists."""


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, factory=_StorageConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        connection.close()
        raise StorageError("SQLite foreign key enforcement could not be enabled")
    return connection


def create_store_pair(operational_path: str | Path, private_path: str | Path) -> StorePair:
    """Initialize operational and private exports at distinct database paths."""
    op_path = Path(operational_path).expanduser().resolve()
    private_db_path = Path(private_path).expanduser().resolve()
    if op_path == private_db_path:
        raise StorageError("operational and private exports must use different database files")
    op_path.parent.mkdir(parents=True, exist_ok=True)
    private_db_path.parent.mkdir(parents=True, exist_ok=True)

    op_connection: sqlite3.Connection | None = None
    private_connection: sqlite3.Connection | None = None
    try:
        op_connection = _connect(op_path)
        private_connection = _connect(private_db_path)
        for statement in OPERATIONAL_DDL:
            op_connection.execute(statement)
        op_connection.execute(
            "INSERT OR IGNORE INTO episodes (episode_id, role) VALUES ('base', 'base')"
        )
        for statement in PRIVATE_DDL:
            private_connection.execute(statement)
        op_connection.commit()
        private_connection.commit()
        return StorePair(op_path, private_db_path, op_connection, private_connection)
    except Exception:
        if op_connection is not None:
            op_connection.close()
        if private_connection is not None:
            private_connection.close()
        raise


def create_export_stores(output_dir: str | Path) -> StorePair:
    """Create the expected operational/ and private/ export layout."""
    root = Path(output_dir)
    return create_store_pair(
        root / "operational" / "observations.sqlite",
        root / "private" / "evaluator.sqlite",
    )


_TABLE_COLUMNS_CACHE_ATTRIBUTE = "_payguard_table_columns"


def _cached_table_columns(connection: sqlite3.Connection, table: str) -> frozenset[str]:
    """Return a table's column names, memoized per connection opened by this module.

    Tables are created before any row is written and this module never alters them, so a
    memoized list cannot go stale on its connection. A missing table is not memoized because
    it may be created later on the same connection. Other connections are queried every time.
    """
    if not isinstance(connection, _StorageConnection):
        return frozenset(table_columns(connection, table))
    cache: dict[str, frozenset[str]] = connection.__dict__.setdefault(
        _TABLE_COLUMNS_CACHE_ATTRIBUTE, {}
    )
    columns = cache.get(table)
    if columns is None:
        columns = frozenset(table_columns(connection, table))
        if columns:
            cache[table] = columns
    return columns


def list_operational_schema(connection: sqlite3.Connection) -> dict[str, tuple[str, ...]]:
    """List every user table and column in an operational export."""
    tables = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return {row[0]: table_columns(connection, row[0]) for row in tables}


def insert_operational_record(
    connection: sqlite3.Connection, table: str, record: Mapping[str, Any]
) -> None:
    """Insert one operational row, canonicalizing timestamps and JSON fields."""
    if table not in OPERATIONAL_TABLES:
        raise StorageError(f"unknown operational table: {table!r}")
    _insert(connection, table, record, OPERATIONAL_TIMESTAMP_COLUMNS.get(table, frozenset()))


def insert_private_record(
    connection: sqlite3.Connection, table: str, record: Mapping[str, Any]
) -> None:
    """Insert one private row; this helper cannot target operational tables."""
    if table not in PRIVATE_TABLES:
        raise StorageError(f"unknown private table: {table!r}")
    _insert(connection, table, record, PRIVATE_TIMESTAMP_COLUMNS.get(table, frozenset()))


def _insert(
    connection: sqlite3.Connection,
    table: str,
    record: Mapping[str, Any],
    timestamp_columns: frozenset[str],
) -> None:
    if not record:
        raise StorageError("record must contain at least one column")
    available_columns = _cached_table_columns(connection, table)
    keys = tuple(record)
    unknown = set(keys) - available_columns
    if unknown:
        raise StorageError(f"unknown columns for {table}: {', '.join(sorted(unknown))}")

    values: list[Any] = []
    json_columns = {
        "expected_effect_json",
        "scope_json",
        "value_json",
        "affected_entities_json",
        "affected_segments_json",
        "affected_features_json",
        "accepted_actions_json",
        "conformal_set",
        "flags",
        "alert_ids_json",
        "checklist_results_json",
        "tool_trace_ids_json",
    }
    for key in keys:
        value = record[key]
        if key in timestamp_columns and value is not None:
            value = utc_timestamp(value)
        elif key in json_columns and not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        elif key in json_columns and isinstance(value, str):
            try:
                json.loads(value)
            except json.JSONDecodeError as exc:
                raise StorageError(f"{key} must contain valid JSON") from exc
        values.append(value)

    column_sql = ", ".join(f'"{key}"' for key in keys)
    placeholders = ", ".join("?" for _ in keys)
    connection.execute(
        f'INSERT INTO "{table}" ({column_sql}) VALUES ({placeholders})', values
    )
