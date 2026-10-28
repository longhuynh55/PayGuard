"""Read-only investigation tools backed by point-in-time operational snapshots."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from simulator.asof import read_as_of
from simulator.schema import AS_OF_TABLES, OPERATIONAL_TABLES, PRIVATE_TABLES, T0_UTC_TEXT
from simulator.storage import utc_timestamp

from triage.llm import LLMCallRecord, LLMClient, LLMConfig, LLMError, LLMResult


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


class _SQLTimeout(Exception):
    """The configured SQL deadline elapsed while executing or fetching."""


class _SQLResultTooLarge(Exception):
    """The materialized SQL result exceeded its configured byte limit."""


class _SQLUnsupportedType(Exception):
    """The materialized SQL result contains a BLOB value, which this tool does not return."""


def run_sql(
    question: str,
    episode_id: str,
    as_of_time: str,
    *,
    operational_db: str | Path,
    client: LLMClient,
    decision_model_slug: str | None = None,
    provider_scoped_decisions: bool = False,
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
            if table == "decisions" and provider_scoped_decisions:
                snapshots[table] = [
                    row
                    for row in snapshots[table]
                    if row["source"] == "rule"
                    or (
                        row["source"] == "agent"
                        and row["model_slug"] == decision_model_slug
                    )
                ]
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
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, tool_config.max_sqlite_value_bytes)
        deadline = time.monotonic() + tool_config.sql_timeout_seconds
        timeout_event = threading.Event()

        def interrupt_after_deadline() -> int:
            if timeout_event.is_set() or time.monotonic() >= deadline:
                timeout_event.set()
                return 1
            return 0

        def interrupt_running_query() -> None:
            timeout_event.set()
            try:
                connection.interrupt()
            except sqlite3.Error:
                pass

        def check_deadline() -> None:
            if timeout_event.is_set() or time.monotonic() >= deadline:
                timeout_event.set()
                raise _SQLTimeout

        connection.set_authorizer(_snapshot_only_authorizer)
        connection.set_progress_handler(interrupt_after_deadline, 100)
        watchdog = threading.Timer(tool_config.sql_timeout_seconds, interrupt_running_query)
        watchdog.daemon = True
        watchdog.start()
        try:
            cursor = connection.execute(sql)
            check_deadline()
            columns = _unique_column_names(cursor.description or ())
            rows: list[dict[str, Any]] = []
            result_bytes = 0
            truncated = False
            for index in range(tool_config.max_result_rows + 1):
                row = cursor.fetchone()
                check_deadline()
                if row is None:
                    break
                if index == tool_config.max_result_rows:
                    truncated = True
                    break
                materialized_row = dict(zip(columns, tuple(row), strict=True))
                if any(isinstance(value, bytes) for value in materialized_row.values()):
                    raise _SQLUnsupportedType
                row_bytes = len(
                    json.dumps(
                        materialized_row,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
                result_bytes += row_bytes
                if result_bytes > tool_config.max_result_bytes:
                    raise _SQLResultTooLarge
                rows.append(materialized_row)
                check_deadline()
        finally:
            watchdog.cancel()
            connection.set_progress_handler(None, 0)
        check_deadline()
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
    except _SQLTimeout:
        return _sql_failure(
            question,
            episode_id,
            normalized_as_of,
            snapshot_hash,
            locals().get("llm_result"),
            "sql_timeout",
            "SQL execution exceeded its time limit.",
        )
    except _SQLResultTooLarge:
        return _sql_failure(
            question,
            episode_id,
            normalized_as_of,
            snapshot_hash,
            locals().get("llm_result"),
            "sql_result_too_large",
            "SQL result exceeded its size limit.",
        )
    except _SQLUnsupportedType:
        return _sql_failure(
            question,
            episode_id,
            normalized_as_of,
            snapshot_hash,
            locals().get("llm_result"),
            "sql_unsupported_type",
            "SQL result contains a BLOB value, which this tool cannot return.",
        )
    except sqlite3.Error:
        timeout_event = locals().get("timeout_event")
        code = "sql_timeout" if timeout_event is not None and timeout_event.is_set() else "sql_execution_error"
        message = (
            "SQL execution exceeded its time limit."
            if code == "sql_timeout"
            else "SQL could not be executed on the approved snapshot."
        )
        return _sql_failure(
            question,
            episode_id,
            normalized_as_of,
            snapshot_hash,
            locals().get("llm_result"),
            code,
            message,
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
    raw_names = [str(column[0] or f"column_{index + 1}") for index, column in enumerate(description)]
    reserved = set(raw_names)
    used: set[str] = set()
    names: list[str] = []
    for raw in raw_names:
        name = raw
        suffix = 2
        while name in used:
            name = f"{raw}_{suffix}"
            suffix += 1
            while name in reserved or name in used:
                name = f"{raw}_{suffix}"
                suffix += 1
        used.add(name)
        names.append(name)
    return names


def _sql_failure(
    question: str,
    episode_id: str,
    as_of_time: str,
    snapshot_hash: str | None,
    llm_result: LLMResult | None,
    code: str,
    message: str,
) -> ToolResult:
    cache_key = llm_result.record.cache_key if llm_result is not None else "no_llm_call"
    trace_id = _trace_id(
        question,
        episode_id,
        as_of_time,
        snapshot_hash or "snapshot_error",
        cache_key,
    )
    sql = None
    if llm_result is not None and llm_result.ok and isinstance(llm_result.value, Mapping):
        candidate = llm_result.value.get("sql")
        sql = candidate if isinstance(candidate, str) else None
    return ToolResult(
        [],
        ToolTrace(
            trace_id,
            sql,
            0,
            False,
            snapshot_hash,
            ToolError(code, message),
            llm_result.record if llm_result is not None else None,
        ),
    )


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
