"""Shared mechanics for timestamps, canonical JSON, JSON manifests and SQLite schema reads.

The helpers here do only the mechanics. Callers keep their own exception types and messages:
the errors raised here carry a ``reason`` that each module maps to the message it shows.
This module imports only the standard library so every package can depend on it.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


class TimestampParseError(ValueError):
    """A value is not an ISO-8601 timestamp with an explicit offset.

    ``reason`` is ``not_text`` (not a string), ``invalid`` (not ISO-8601), ``naive`` (no
    offset) or ``not_utc`` (an offset other than UTC, only when ``utc=True``).
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def parse_timestamp(value: object, *, utc: bool = False) -> datetime:
    """Parse ISO-8601 text into an aware datetime converted to UTC.

    A trailing ``Z`` means UTC. Naive values are rejected rather than read in the machine's
    local time zone. With ``utc=True`` any offset other than UTC is rejected too.
    """
    if not isinstance(value, str):
        raise TimestampParseError("not_text", "timestamp must be text")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise TimestampParseError("invalid", str(exc)) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TimestampParseError("naive", "timestamp must include a timezone")
    if utc and parsed.utcoffset() != timedelta(0):
        raise TimestampParseError("not_utc", "timestamp must be UTC")
    return parsed.astimezone(UTC)


def format_timestamp(value: datetime) -> str:
    """Render an aware datetime as fixed-width UTC text, e.g. ``2026-02-01T00:00:00.000000Z``."""
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    """Compact JSON with sorted keys, used for every hash, fingerprint and identity.

    NaN and Infinity are rejected (``allow_nan=False``), so one value has one encoding.
    """
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def canonical_json_bytes(value: Any) -> bytes:
    """UTF-8 bytes of :func:`canonical_json`."""
    return canonical_json(value).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    """SHA-256 hex digest of :func:`canonical_json_bytes`."""
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


class JsonFileError(ValueError):
    """A JSON manifest is unreadable or its top level is not an object.

    ``reason`` is ``unreadable`` or ``not_object``.
    """

    def __init__(self, reason: str, path: Path) -> None:
        super().__init__(f"{reason}: {path}")
        self.reason = reason
        self.path = path


def read_json_object(path: Path) -> dict[str, Any]:
    """Read a UTF-8 JSON file whose top-level value must be an object."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise JsonFileError("unreadable", path) from exc
    if not isinstance(value, dict):
        raise JsonFileError("not_object", path)
    return value


def table_columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    """Column names of ``table`` in declaration order (``PRAGMA table_info``)."""
    escaped = table.replace('"', '""')
    rows = connection.execute(f'PRAGMA table_info("{escaped}")').fetchall()
    return tuple(row[1] for row in rows)


def primary_key_columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    """Primary-key column names in key order; empty when ``table`` has no primary key."""
    escaped = table.replace('"', '""')
    rows = connection.execute(f'PRAGMA table_info("{escaped}")').fetchall()
    return tuple(row[1] for row in sorted(rows, key=lambda row: row[5]) if row[5])
