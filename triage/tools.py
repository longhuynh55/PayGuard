"""Read-only investigation tools backed by point-in-time operational snapshots."""

from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from simulator.asof import read_as_of
from simulator.schema import AS_OF_TABLES, OPERATIONAL_TABLES, PRIVATE_TABLES, T0_UTC_TEXT
from simulator.storage import StorageError, utc_timestamp

from triage.llm import LLMCallRecord, LLMClient, LLMConfig, LLMError, LLMResult

logger = logging.getLogger(__name__)


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


# Agent-facing messages. They never include paths, SQL text, or exception text.
_SNAPSHOT_MESSAGES = {
    "invalid_as_of": "The requested as-of snapshot is invalid.",
    "operational_db_invalid": "The database is not an isolated operational export.",
    "snapshot_source_unavailable": "The operational snapshot source could not be read.",
    "snapshot_scope_invalid": "The requested episode or as-of scope is not available.",
    "snapshot_schema_unsupported": "The operational snapshot schema is not supported by this tool.",
    "snapshot_value_unsupported": "The operational snapshot contains a value this tool cannot serialize.",
    "snapshot_internal_error": "The as-of operational snapshot could not be prepared.",
}
# Declared column types of the operational schema (TEXT, INTEGER, REAL), optionally sized.
_DECLARED_TYPE = re.compile(r"[A-Za-z][A-Za-z ]*(\([0-9]+(,[0-9]+)?\))?")


class _SnapshotBuildError(Exception):
    """A classified failure while preparing the snapshot; ``code`` is safe to return to the agent."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
        self.message = _SNAPSHOT_MESSAGES[code]


def _fail(code: str, stage: str, cause: BaseException | None = None) -> _SnapshotBuildError:
    """Record internal diagnostics (stage and exception type only) and return the safe error."""
    logger.warning(
        "run_sql snapshot failure code=%s stage=%s cause=%s",
        code,
        stage,
        type(cause).__name__ if cause is not None else "none",
    )
    return _SnapshotBuildError(code)


@contextmanager
def _sqlite_stage(stage: str) -> Iterator[None]:
    try:
        yield
    except sqlite3.Error as exc:
        raise _fail("snapshot_source_unavailable", stage, exc) from exc


@dataclass(frozen=True)
class _SnapshotImage:
    """One immutable as-of snapshot: prompt schema, content hash, and a serialized SQLite image."""

    data_version: int
    schema_text: str
    snapshot_hash: str
    image: bytes


class SnapshotCache:
    """Share built as-of snapshots between tool calls while the operational data is unchanged.

    One read-only probe connection per database file reads ``PRAGMA data_version``. SQLite
    advances that value for every commit made by another connection, so an entry is reused only
    when nothing was committed since it was built. A decision written at a cutoff is visible at
    that cutoff, so the next question at that cutoff rebuilds; questions with no commit in between
    share one build. Entries are immutable, and every tool call still runs its SQL on its own
    connection.
    """

    def __init__(self, *, max_entries: int = 1) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self._max_entries = max_entries
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None
        self._path: Path | None = None
        self._identity: tuple[int, int] | None = None
        self._entries: OrderedDict[tuple[str, str], _SnapshotImage] = OrderedDict()
        self.builds = 0
        self.reuses = 0

    def snapshot(self, operational_path: Path, episode_id: str, as_of_time: str) -> _SnapshotImage:
        with self._lock:
            connection = self._probe(operational_path)
            _require_operational_export(connection)
            with _sqlite_stage("data_version"):
                current_version = _data_version(connection)
            key = (episode_id, as_of_time)
            cached = self._entries.get(key)
            if cached is not None and cached.data_version == current_version:
                self._entries.move_to_end(key)
                self.reuses += 1
                return cached
            built = _build_snapshot(connection, episode_id, as_of_time)
            self.builds += 1
            self._entries[key] = built
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
            return built

    def close(self) -> None:
        with self._lock:
            self._entries.clear()
            if self._connection is not None:
                self._connection.close()
            self._connection = None
            self._path = None
            self._identity = None

    def _probe(self, operational_path: Path) -> sqlite3.Connection:
        try:
            stat = operational_path.stat()
        except OSError as exc:
            raise _fail("snapshot_source_unavailable", "probe_stat", exc) from exc
        identity = (stat.st_dev, stat.st_ino)
        if self._connection is None or self._path != operational_path or self._identity != identity:
            # A replaced file has a new inode; drop entries built from the old one.
            self.close()
            with _sqlite_stage("probe_open"):
                self._connection = _open_read_only(operational_path)
            self._path = operational_path
            self._identity = identity
        return self._connection


def run_sql(
    question: str,
    episode_id: str,
    as_of_time: str,
    *,
    operational_db: str | Path,
    client: LLMClient,
    snapshot_cache: SnapshotCache | None = None,
) -> ToolResult:
    """Generate one SQL statement, then run it only against a read-only snapshot.

    Calls that pass the same ``snapshot_cache`` share one built snapshot per episode and cutoff
    until the operational data changes. Without a cache, each call builds its own snapshot.
    """
    tool_config: LLMConfig = client.config
    normalized_as_of = as_of_time
    try:
        normalized_as_of = utc_timestamp(as_of_time)
    except (TypeError, ValueError):
        return _failed_result(
            question,
            episode_id,
            normalized_as_of,
            "invalid_as_of",
            _SNAPSHOT_MESSAGES["invalid_as_of"],
        )
    if not isinstance(question, str) or not question.strip():
        return _failed_result(
            str(question), episode_id, normalized_as_of, "invalid_question", "Question must not be empty."
        )
    if not isinstance(episode_id, str) or not episode_id or episode_id == "base":
        return _failed_result(
            question, str(episode_id), normalized_as_of, "invalid_episode", "A registered non-base episode is required."
        )

    owned_cache = snapshot_cache is None
    cache = SnapshotCache() if snapshot_cache is None else snapshot_cache
    connection: sqlite3.Connection | None = None
    try:
        operational_path = _resolve_operational_path(operational_db)
        snapshot = cache.snapshot(operational_path, episode_id, normalized_as_of)
    except _SnapshotBuildError as exc:
        return _failed_result(question, episode_id, normalized_as_of, exc.code, exc.message)
    except Exception as exc:
        failure = _fail("snapshot_internal_error", "prepare", exc)
        return _failed_result(question, episode_id, normalized_as_of, failure.code, failure.message)
    finally:
        if owned_cache:
            cache.close()

    snapshot_hash = snapshot.snapshot_hash
    try:
        prompt = (
            "Return exactly one JSON object with one string field named sql. The value must be one "
            "read-only SQLite SELECT or WITH query over the supplied as-of snapshot tables. "
            "Use only the listed tables and columns; qualify tables with the asof schema. "
            "Do not use ATTACH, PRAGMA, comments containing another statement, or any write operation.\n\n"
            f"Available tables:\n{snapshot.schema_text}\n\nQuestion: {question.strip()}"
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

        connection = _open_execution_connection(operational_path, snapshot)
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
    except _SnapshotBuildError as exc:
        return _sql_failure(
            question,
            episode_id,
            normalized_as_of,
            snapshot_hash,
            locals().get("llm_result"),
            exc.code,
            exc.message,
        )
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
                ToolError("tool_internal_error", "The SQL tool failed before returning a result."),
                llm_record.record if llm_record else None,
            ),
        )
    finally:
        if connection is not None:
            connection.close()


def _resolve_operational_path(operational_db: str | Path) -> Path:
    try:
        return Path(operational_db).expanduser().resolve()
    except (TypeError, ValueError, OSError, RuntimeError) as exc:
        raise _fail("snapshot_source_unavailable", "resolve_path", exc) from exc


def _open_read_only(operational_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{operational_path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _require_operational_export(connection: sqlite3.Connection) -> None:
    with _sqlite_stage("operational_schema"):
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
    if not OPERATIONAL_TABLES.issubset(tables) or tables.intersection(PRIVATE_TABLES):
        raise _SnapshotBuildError("operational_db_invalid")


def _data_version(connection: sqlite3.Connection) -> int:
    cursor = connection.execute("PRAGMA data_version")
    try:
        return int(cursor.fetchall()[0][0])
    finally:
        cursor.close()


def _build_snapshot(connection: sqlite3.Connection, episode_id: str, as_of_time: str) -> _SnapshotImage:
    """Read every as-of table in one read transaction, then serialize them with declared types."""
    with _sqlite_stage("begin"):
        connection.execute("BEGIN")
    try:
        with _sqlite_stage("data_version"):
            data_version = _data_version(connection)
        snapshots: dict[str, list[dict[str, Any]]] = {}
        declared: dict[str, tuple[tuple[str, str], ...]] = {}
        for table in sorted(AS_OF_TABLES):
            snapshots[table] = _read_table(connection, table, episode_id, as_of_time)
            declared[table] = _declared_columns(connection, table)
        try:
            snapshot_hash = _snapshot_hash(snapshots)
        except (TypeError, ValueError) as exc:
            raise _fail("snapshot_value_unsupported", "hash", exc) from exc
        schema_text = "\n".join(
            f"- asof.{table}({', '.join(name for name, _ in declared[table])})"
            for table in sorted(declared)
        )
        image = _serialize_tables(snapshots, declared)
        with _sqlite_stage("commit"):
            connection.commit()
    except BaseException:
        try:
            if connection.in_transaction:
                connection.rollback()
        except sqlite3.Error:
            pass
        raise
    return _SnapshotImage(data_version, schema_text, snapshot_hash, image)


def _read_table(
    connection: sqlite3.Connection, table: str, episode_id: str, as_of_time: str
) -> list[dict[str, Any]]:
    try:
        return read_as_of(
            connection,
            table,
            episode_id=episode_id,
            t0=T0_UTC_TEXT,
            as_of_time=as_of_time,
        )
    except sqlite3.Error as exc:
        raise _fail("snapshot_source_unavailable", f"read:{table}", exc) from exc
    except (StorageError, ValueError) as exc:
        raise _fail("snapshot_scope_invalid", f"read:{table}", exc) from exc
    except Exception as exc:
        raise _fail("snapshot_internal_error", f"read:{table}", exc) from exc


def _declared_columns(connection: sqlite3.Connection, table: str) -> tuple[tuple[str, str], ...]:
    """Return (column name, declared type) in source order; the declared type sets SQLite affinity."""
    with _sqlite_stage(f"schema:{table}"):
        rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    columns = tuple((str(row[1]), str(row[2] or "")) for row in rows)
    for _, declared_type in columns:
        if declared_type and _DECLARED_TYPE.fullmatch(declared_type) is None:
            raise _fail("snapshot_schema_unsupported", f"schema:{table}")
    return columns


def _quote_identifier(name: str) -> str:
    return name.replace('"', '""')


def _serialize_tables(
    snapshots: Mapping[str, list[dict[str, Any]]],
    declared: Mapping[str, tuple[tuple[str, str], ...]],
) -> bytes:
    """Return a SQLite image whose columns keep the source declared types, hence source affinity.

    A column declared BLOB would compare '123' and 123 as different values, unlike the source
    table. Declared types come from PRAGMA table_info and were checked by ``_declared_columns``.
    """
    builder = sqlite3.connect(":memory:")
    try:
        for table in sorted(declared):
            columns = declared[table]
            quoted_table = _quote_identifier(table)
            definitions = ", ".join(
                f'"{_quote_identifier(name)}"' + (f" {declared_type}" if declared_type else "")
                for name, declared_type in columns
            )
            builder.execute(f'CREATE TABLE "{quoted_table}" ({definitions})')
            rows = snapshots[table]
            if rows:
                column_sql = ", ".join(f'"{_quote_identifier(name)}"' for name, _ in columns)
                placeholders = ", ".join("?" for _ in columns)
                builder.executemany(
                    f'INSERT INTO "{quoted_table}" ({column_sql}) VALUES ({placeholders})',
                    (tuple(row.get(name) for name, _ in columns) for row in rows),
                )
        return builder.serialize()
    except sqlite3.Error as exc:
        raise _fail("snapshot_internal_error", "serialize", exc) from exc
    finally:
        builder.close()


def _open_execution_connection(operational_path: Path, snapshot: _SnapshotImage) -> sqlite3.Connection:
    """Open the per-call connection: operational main (authorizer-denied) plus the asof image."""
    with _sqlite_stage("execution_open"):
        connection = _open_read_only(operational_path)
    try:
        connection.execute("ATTACH DATABASE ':memory:' AS asof")
        connection.deserialize(snapshot.image, name="asof")
    except sqlite3.Error as exc:
        connection.close()
        raise _fail("snapshot_internal_error", "execution_attach", exc) from exc
    return connection


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
