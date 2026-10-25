"""The sole operational reader for episode-scoped, point-in-time data."""

from __future__ import annotations

import sqlite3
from typing import Any

from simulator.schema import AS_OF_TABLES
from simulator.storage import StorageError, utc_timestamp


def read_as_of(
    connection: sqlite3.Connection,
    table: str,
    *,
    episode_id: str,
    t0: str,
    as_of_time: str,
) -> list[dict[str, Any]]:
    """Read one operational table under the shared-base and availability contract.

    This is the only API for reading operational rows for model/monitoring use.
    Table names are allowlisted because SQL identifiers cannot be bound as values.
    """
    if table not in AS_OF_TABLES:
        raise StorageError(f"table is not an as-of operational table: {table!r}")
    if not episode_id or episode_id == "base":
        raise StorageError("episode_id must identify a registered episode other than 'base'")

    t0_utc = utc_timestamp(t0)
    as_of_utc = utc_timestamp(as_of_time)
    registered = connection.execute(
        "SELECT 1 FROM episodes WHERE episode_id = ? AND episode_id <> 'base'",
        (episode_id,),
    ).fetchone()
    if registered is None:
        raise StorageError(f"episode is not registered: {episode_id!r}")

    rows = connection.execute(
        f"""
        SELECT * FROM "{table}"
        WHERE available_at <= ?
          AND ((episode_id = 'base' AND event_at < ?) OR episode_id = ?)
        ORDER BY event_at, rowid
        """,
        (as_of_utc, t0_utc, episode_id),
    ).fetchall()
    return [dict(row) for row in rows]
