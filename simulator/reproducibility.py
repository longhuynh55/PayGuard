"""Canonical content fingerprints for generated operational observations."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from typing import Any

from simulator.schema import OPERATIONAL_TABLES
from simulator.storage import insert_private_record


P1_3_FINGERPRINT_TABLES = (
    "customers",
    "merchants",
    "transactions",
    "transaction_events",
)
FINGERPRINT_METADATA_KEY = "p1.3.canonical_fingerprint.v1"


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
    payload = json.dumps(
        canonical_content(connection, tables),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


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
    serialized = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
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
