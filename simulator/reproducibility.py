"""Canonical content fingerprints for generated operational observations."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import Any

from simulator.common import canonical_json, canonical_sha256
from simulator.schema import OPERATIONAL_TABLES
from simulator.storage import insert_private_record, utc_timestamp


P1_3_FINGERPRINT_TABLES = (
    "customers",
    "merchants",
    "transactions",
    "transaction_events",
)
FINGERPRINT_METADATA_KEY = "p1.3.canonical_fingerprint.v1"
RUN_FINGERPRINT_METADATA_KEY = "p1.8.run_canonical_fingerprint.v1"
EPISODE_FINGERPRINT_TABLES = (
    "customers",
    "merchants",
    "transactions",
    "transaction_events",
    "label_events",
    "business_calendar",
)


def canonical_content(
    connection: sqlite3.Connection,
    tables: Sequence[str] = P1_3_FINGERPRINT_TABLES,
) -> dict[str, list[dict[str, Any]]]:
    """Read rows in stable table/primary-key order, independent of DB layout."""
    if len(tables) != len(set(tables)):
        raise ValueError("canonical fingerprint tables must not contain duplicates")
    if any(table not in OPERATIONAL_TABLES or table == "episodes" for table in tables):
        raise ValueError("canonical fingerprint accepts only P1 operational data tables")

    content: dict[str, list[dict[str, Any]]] = {}
    for table in sorted(tables):
        escaped = table.replace('"', '""')
        column_info = connection.execute(f'PRAGMA table_info("{escaped}")').fetchall()
        if not column_info:
            raise ValueError(f"operational table does not exist: {table}")
        columns = [row[1] for row in column_info]
        primary_key = [row[1] for row in sorted(column_info, key=lambda row: row[5]) if row[5]]
        if not primary_key:
            raise ValueError(f"canonical table must have a primary key: {table}")
        order_sql = ", ".join(f'"{column}"' for column in primary_key)
        rows = connection.execute(
            f'SELECT * FROM "{escaped}" ORDER BY {order_sql}'
        ).fetchall()
        content[table] = [
            {column: row[column] for column in columns}
            for row in rows
        ]
    return content


def canonical_fingerprint(
    connection: sqlite3.Connection,
    tables: Sequence[str] = P1_3_FINGERPRINT_TABLES,
) -> str:
    """Return SHA-256 of canonical row content, never the SQLite file bytes."""
    return canonical_sha256(canonical_content(connection, tables))


def canonical_run_content(connection: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    """Return every operational row in stable table and primary-key order."""
    content: dict[str, list[dict[str, Any]]] = {}
    for table in sorted(OPERATIONAL_TABLES):
        escaped = table.replace('"', '""')
        column_info = connection.execute(f'PRAGMA table_info("{escaped}")').fetchall()
        if not column_info:
            raise ValueError(f"operational table does not exist: {table}")
        columns = [row[1] for row in column_info]
        primary_key = [row[1] for row in sorted(column_info, key=lambda row: row[5]) if row[5]]
        if not primary_key:
            raise ValueError(f"canonical table must have a primary key: {table}")
        order_sql = ", ".join(f'"{column}"' for column in primary_key)
        rows = connection.execute(
            f'SELECT * FROM "{escaped}" ORDER BY {order_sql}'
        ).fetchall()
        content[table] = [{column: row[column] for column in columns} for row in rows]
    return content


def canonical_run_fingerprint(
    connection: sqlite3.Connection,
    *,
    empty_tables: Sequence[str] = (),
) -> str:
    """Hash the operational table set, optionally treating derived outputs as empty.

    ``empty_tables`` supports validating a generated-run fingerprint after later
    stages have populated their output tables. Those tables were empty when the
    smoke run was generated, so their current rows do not change its fingerprint.
    """
    if len(empty_tables) != len(set(empty_tables)):
        raise ValueError("canonical run empty_tables must not contain duplicates")
    if any(table not in OPERATIONAL_TABLES for table in empty_tables):
        raise ValueError("canonical run empty_tables must name operational tables")
    content = canonical_run_content(connection)
    for table in empty_tables:
        content[table] = []
    return canonical_sha256(content)


def canonical_episode_content(
    operational: sqlite3.Connection,
    *,
    episode_id: str,
    t0: str,
) -> dict[str, list[dict[str, Any]]]:
    """Return canonical shared-history plus one episode's operational rows."""
    if not episode_id or episode_id == "base":
        raise ValueError("episode fingerprint requires a non-base episode_id")
    t0_utc = utc_timestamp(t0)
    if operational.execute(
        "SELECT 1 FROM episodes WHERE episode_id = ? AND role <> 'base'", (episode_id,)
    ).fetchone() is None:
        raise ValueError(f"episode is not registered: {episode_id!r}")

    operational_content: dict[str, list[dict[str, Any]]] = {}
    for table in sorted(EPISODE_FINGERPRINT_TABLES):
        column_info = operational.execute(f'PRAGMA table_info("{table}")').fetchall()
        columns = [row[1] for row in column_info]
        primary_key = [row[1] for row in sorted(column_info, key=lambda row: row[5]) if row[5]]
        if not columns or not primary_key:
            raise ValueError(f"operational table is missing or has no primary key: {table}")
        order_sql = ", ".join(f'"{column}"' for column in primary_key)
        rows = operational.execute(
            f'SELECT * FROM "{table}" '
            "WHERE (episode_id = 'base' AND event_at < ?) OR episode_id = ? "
            f"ORDER BY {order_sql}",
            (t0_utc, episode_id),
        ).fetchall()
        operational_content[table] = [
            {column: row[column] for column in columns} for row in rows
        ]

    return operational_content


def canonical_episode_fingerprint(
    operational: sqlite3.Connection,
    *,
    episode_id: str,
    t0: str,
) -> str:
    """Return SHA-256 for one canonical operational episode view."""
    return canonical_sha256(
        canonical_episode_content(operational, episode_id=episode_id, t0=t0)
    )


def record_canonical_fingerprint(
    operational: sqlite3.Connection,
    private: sqlite3.Connection,
    tables: Sequence[str] = P1_3_FINGERPRINT_TABLES,
) -> str:
    """Store the operational content fingerprint in private run metadata."""
    fingerprint = canonical_fingerprint(operational, tables)
    metadata = {
        "algorithm": "sha256",
        "canonicalization": "sorted tables, primary-key rows, sorted JSON object keys; v1",
        "tables": sorted(tables),
        "fingerprint": fingerprint,
    }
    serialized = canonical_json(metadata)
    existing = private.execute(
        "SELECT 1 FROM run_metadata WHERE key = ?", (FINGERPRINT_METADATA_KEY,)
    ).fetchone()
    if existing:
        private.execute(
            "UPDATE run_metadata SET value_json = ? WHERE key = ?",
            (serialized, FINGERPRINT_METADATA_KEY),
        )
    else:
        insert_private_record(
            private,
            "run_metadata",
            {"key": FINGERPRINT_METADATA_KEY, "value_json": metadata},
        )
    return fingerprint
