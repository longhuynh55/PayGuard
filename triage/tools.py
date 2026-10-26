"""Read-only investigation tools backed by point-in-time operational snapshots."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from simulator.asof import read_as_of
from simulator.schema import AS_OF_TABLES, OPERATIONAL_TABLES, PRIVATE_TABLES, T0_UTC_TEXT
from simulator.storage import utc_timestamp

from triage.llm import LLMCallRecord, LLMClient, LLMConfig, LLMError


@dataclass(frozen=True)
class ToolError:
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class ToolTrace:
    tool_trace_id: str
    sql: str | None
    row_count: int
    truncated: bool
    data_snapshot: str | None
    error: ToolError | None
    llm: LLMCallRecord | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool_trace_id": self.tool_trace_id,
            "sql": self.sql,
            "row_count": self.row_count,
            "truncated": self.truncated,
            "data_snapshot": self.data_snapshot,
            "error": self.error.as_dict() if self.error else None,
            "llm": self.llm.as_dict() if self.llm else None,
        }


@dataclass(frozen=True)
class ToolResult:
    rows: list[dict[str, Any]]
    trace: ToolTrace

    @property
    def ok(self) -> bool:
        return self.trace.error is None

    @property
    def error(self) -> ToolError | None:
        return self.trace.error


def run_sql(
    question: str,
    episode_id: str,
    as_of_time: str,
    *,
    operational_db: str | Path,
    client: LLMClient,
) -> ToolResult:
    """Generate one SQL statement, then run it only against a read-only snapshot."""
    tool_config: LLMConfig = client.config
    normalized_as_of = as_of_time
    try:
        normalized_as_of = utc_timestamp(as_of_time)
    except (TypeError, ValueError):
        return _failed_result(
            question,
            episode_id,
            normalized_as_of,
            "snapshot_error",
            "The requested as-of snapshot is invalid.",
        )
    if not isinstance(question, str) or not question.strip():
        return _failed_result(
            str(question), episode_id, normalized_as_of, "invalid_question", "Question must not be empty."
        )
    if not isinstance(episode_id, str) or not episode_id or episode_id == "base":
        return _failed_result(
            question, str(episode_id), normalized_as_of, "invalid_episode", "A registered non-base episode is required."
        )

    connection: sqlite3.Connection | None = None
    snapshot_hash: str | None = None
    try:
        operational_path = Path(operational_db).expanduser().resolve()
        uri = f"{operational_path.as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if not OPERATIONAL_TABLES.issubset(tables) or tables.intersection(PRIVATE_TABLES):
            return _failed_result(
                question,
                episode_id,
                normalized_as_of,
                "operational_db_invalid",
                "The database is not an isolated operational export.",
            )

        snapshots: dict[str, list[dict[str, Any]]] = {}
        schemas: dict[str, tuple[str, ...]] = {}
        for table in sorted(AS_OF_TABLES):
            snapshots[table] = read_as_of(
                connection,
                table,
                episode_id=episode_id,
                t0=T0_UTC_TEXT,
                as_of_time=normalized_as_of,
            )
            schemas[table] = tuple(
                str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')
            )
        snapshot_hash = _snapshot_hash(snapshots)
        schema_text = "\n".join(
            f"- asof.{table}({', '.join(schemas[table])})" for table in sorted(schemas)
        )
        prompt = (
            "Return exactly one JSON object with one string field named sql. The value must be one "
            "read-only SQLite SELECT or WITH query over the supplied as-of snapshot tables. "
            "Use only the listed tables and columns; qualify tables with the asof schema. "
            "Do not use ATTACH, PRAGMA, comments containing another statement, or any write operation.\n\n"
            f"Available tables:\n{schema_text}\n\nQuestion: {question.strip()}"
        )
        llm_result = client.complete_json(
            prompt,
            episode_id=episode_id,
            as_of_time=normalized_as_of,
            data_snapshot=snapshot_hash,
        )
        trace_id = _trace_id(
            question,
            episode_id,
            normalized_as_of,
            snapshot_hash,
            llm_result.record.cache_key,
        )
        if not llm_result.ok:
            error = _llm_error(llm_result.error)
            return ToolResult(
                [],
                ToolTrace(trace_id, None, 0, False, snapshot_hash, error, llm_result.record),
            )
        if not isinstance(llm_result.value, Mapping) or set(llm_result.value) != {"sql"}:
            error = ToolError("invalid_model_output", "LLM output must contain one SQL string.")
            return ToolResult(
                [],
                ToolTrace(trace_id, None, 0, False, snapshot_hash, error, llm_result.record),
            )
        sql = llm_result.value.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            error = ToolError("invalid_model_output", "LLM output must contain one SQL string.")
            return ToolResult(
                [],
                ToolTrace(trace_id, None, 0, False, snapshot_hash, error, llm_result.record),
            )
        if not _is_select_or_with(sql):
            error = ToolError("sql_not_read_only", "Only one SELECT or WITH query is allowed.")
            return ToolResult(
                [],
                ToolTrace(trace_id, sql, 0, False, snapshot_hash, error, llm_result.record),
            )

        _attach_asof_snapshot(connection, schemas, snapshots)
        connection.execute("PRAGMA query_only = ON")
        deadline = time.monotonic() + tool_config.sql_timeout_seconds
        timed_out = False

        def interrupt_after_deadline() -> int:
            nonlocal timed_out
            if time.monotonic() >= deadline:
                timed_out = True
                return 1
            return 0

        connection.set_authorizer(_snapshot_only_authorizer)
        connection.set_progress_handler(interrupt_after_deadline, 1000)
        cursor = connection.execute(sql)
        columns = _unique_column_names(cursor.description or ())
        fetched = cursor.fetchmany(tool_config.max_result_rows + 1)
        truncated = len(fetched) > tool_config.max_result_rows
        limited = fetched[: tool_config.max_result_rows]
        rows = [dict(zip(columns, tuple(row), strict=True)) for row in limited]
        trace = ToolTrace(
            trace_id,
            sql,
            len(rows),
            truncated,
            snapshot_hash,
            None,
            llm_result.record,
        )
        return ToolResult(rows, trace)
    except sqlite3.Error:
        code = "sql_timeout" if "timed_out" in locals() and timed_out else "sql_execution_error"
        message = (
            "SQL execution exceeded its time limit."
            if code == "sql_timeout"
            else "SQL could not be executed on the approved snapshot."
        )
        error = ToolError(code, message)
        llm_record = locals().get("llm_result")
        trace_id = _trace_id(
            question,
            episode_id,
            normalized_as_of,
            snapshot_hash or "snapshot_error",
            llm_record.record.cache_key if llm_record else "no_llm_call",
        )
        sql = None
        if llm_record and llm_record.ok and isinstance(llm_record.value, Mapping):
            candidate = llm_record.value.get("sql")
            sql = candidate if isinstance(candidate, str) else None
        return ToolResult(
            [], ToolTrace(trace_id, sql, 0, False, snapshot_hash, error, llm_record.record if llm_record else None)
        )
    except Exception:
        # Do not return exception text: it can contain filesystem paths or schema details.
        llm_record = locals().get("llm_result")
        trace_id = _trace_id(
            question,
            episode_id,
            normalized_as_of,
            snapshot_hash or "snapshot_error",
            llm_record.record.cache_key if llm_record else "no_llm_call",
        )
        return ToolResult(
            [],
            ToolTrace(
                trace_id,
                None,
                0,
                False,
                snapshot_hash,
                ToolError("snapshot_error", "The as-of operational snapshot could not be prepared."),
                llm_record.record if llm_record else None,
            ),
        )
    finally:
        if connection is not None:
            connection.close()


def _snapshot_hash(snapshots: Mapping[str, list[dict[str, Any]]]) -> str:
    payload = json.dumps(
        {table: rows for table, rows in sorted(snapshots.items())},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _trace_id(
    question: str,
    episode_id: str,
    as_of_time: str,
    data_snapshot: str,
    cache_key: str,
) -> str:
    payload = json.dumps(
        {
            "tool": "run_sql.v1",
            "question": question,
            "episode_id": episode_id,
            "as_of_time": as_of_time,
            "data_snapshot": data_snapshot,
            "cache_key": cache_key,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _is_select_or_with(sql: str) -> bool:
    if not sql or "\x00" in sql:
        return False
    candidate = sql.lstrip()
    while True:
        if candidate.startswith("--"):
            newline = candidate.find("\n")
            if newline < 0:
                return False
            candidate = candidate[newline + 1 :].lstrip()
            continue
        if candidate.startswith("/*"):
            end = candidate.find("*/", 2)
            if end < 0:
                return False
            candidate = candidate[end + 2 :].lstrip()
            continue
        break
    return re.match(r"(?is)^(SELECT|WITH)\b", candidate) is not None


def _attach_asof_snapshot(
    connection: sqlite3.Connection,
    schemas: Mapping[str, tuple[str, ...]],
    snapshots: Mapping[str, list[dict[str, Any]]],
) -> None:
    connection.execute("ATTACH DATABASE ':memory:' AS asof")
    for table in sorted(schemas):
        columns = schemas[table]
        quoted_columns = ", ".join(f'"{column.replace(chr(34), chr(34) * 2)}" BLOB' for column in columns)
        connection.execute(f'CREATE TABLE asof."{table}" ({quoted_columns})')
        if not snapshots[table]:
            continue
        column_sql = ", ".join(f'"{column.replace(chr(34), chr(34) * 2)}"' for column in columns)
        placeholders = ", ".join("?" for _ in columns)
        values = [tuple(row.get(column) for column in columns) for row in snapshots[table]]
        connection.executemany(
            f'INSERT INTO asof."{table}" ({column_sql}) VALUES ({placeholders})', values
        )


def _snapshot_only_authorizer(
    action: int, arg1: str | None, arg2: str | None, database: str | None, _trigger: str | None
) -> int:
    if action in {sqlite3.SQLITE_SELECT, getattr(sqlite3, "SQLITE_RECURSIVE", -1)}:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ:
        if database == "asof" and arg1 in AS_OF_TABLES:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION:
        function = (arg2 or arg1 or "").casefold()
        if function in {"load_extension", "readfile", "writefile", "fts3_tokenizer"}:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _unique_column_names(description: tuple[Any, ...]) -> list[str]:
    seen: dict[str, int] = {}
    names: list[str] = []
    for index, column in enumerate(description):
        raw = str(column[0] or f"column_{index + 1}")
        count = seen.get(raw, 0)
        seen[raw] = count + 1
        names.append(raw if count == 0 else f"{raw}_{count + 1}")
    return names


def _llm_error(error: LLMError | None) -> ToolError:
    if error is None:
        return ToolError("llm_error", "LLM request did not complete.")
    return ToolError(error.code, error.message)


def _failed_result(
    question: str, episode_id: str, as_of_time: str, code: str, message: str
) -> ToolResult:
    trace_id = _trace_id(question, episode_id, as_of_time, "snapshot_error", "no_llm_call")
    return ToolResult(
        [], ToolTrace(trace_id, None, 0, False, None, ToolError(code, message), None)
    )
