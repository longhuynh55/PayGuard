from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import sqlite3

from simulator.storage import StorageError, insert_operational_record
from triage.llm import FakeLLMClient
from triage.tools import SnapshotCache, run_sql


AS_OF = "2026-02-02T00:00:00Z"


def _run(store, sql: str, *, question: str = "Count visible transactions"):
    return run_sql(
        question,
        "ep-a",
        AS_OF,
        operational_db=store.operational_path,
        client=FakeLLMClient(response={"sql": sql}),
    )


def test_sql_reads_only_the_asof_episode_snapshot(p1_2_store) -> None:
    result = _run(
        p1_2_store,
        "SELECT txn_id, episode_id FROM asof.transactions ORDER BY txn_id",
    )

    assert result.ok
    assert {row["txn_id"] for row in result.rows} == {
        "txn-base-before-t0",
        "txn-base-at-boundary",
        "txn-a-at-availability-cutoff",
    }
    assert "txn-a-not-yet-available" not in {row["txn_id"] for row in result.rows}
    assert "txn-b-other-episode" not in {row["txn_id"] for row in result.rows}
    assert result.trace.row_count == 3
    assert result.trace.sql == "SELECT txn_id, episode_id FROM asof.transactions ORDER BY txn_id"
    assert result.trace.data_snapshot
    assert result.trace.llm and result.trace.llm.cache_key
    assert result.trace.tool_trace_id == _run(
        p1_2_store,
        "SELECT txn_id, episode_id FROM asof.transactions ORDER BY txn_id",
    ).trace.tool_trace_id


def test_write_attach_and_pragma_statements_are_rejected(p1_2_store) -> None:
    statements = [
        "INSERT INTO asof.transactions (txn_id) VALUES ('x')",
        "UPDATE asof.transactions SET amount = 0",
        "ATTACH DATABASE ':memory:' AS leak",
        "PRAGMA table_info(transactions)",
    ]

    results = [_run(p1_2_store, statement) for statement in statements]

    assert all(not result.ok for result in results)
    assert [result.error.code for result in results] == ["sql_not_read_only"] * 4


def test_truth_database_attach_is_rejected_by_run_sql(p1_2_store) -> None:
    private_path = p1_2_store.private_path
    before = hashlib.sha256(private_path.read_bytes()).hexdigest()
    escaped_path = str(private_path).replace("'", "''")
    result = _run(
        p1_2_store,
        f"ATTACH DATABASE '{escaped_path}' AS truth",
        question="Attach the evaluator truth database",
    )

    assert result.error and result.error.code == "sql_not_read_only"
    assert hashlib.sha256(private_path.read_bytes()).hexdigest() == before


def test_readonly_authorizer_blocks_main_database_and_multiple_statements(p1_2_store) -> None:
    direct_main = _run(p1_2_store, "SELECT * FROM main.transactions")
    multiple = _run(p1_2_store, "SELECT 1; SELECT 2")

    assert direct_main.error and direct_main.error.code == "sql_execution_error"
    assert multiple.error and multiple.error.code == "sql_execution_error"


def test_llm_and_sql_failures_are_structured_and_do_not_expose_paths(p1_2_store) -> None:
    llm_failure = run_sql(
        "List transactions",
        "ep-a",
        AS_OF,
        operational_db=p1_2_store.operational_path,
        client=FakeLLMClient(response="not-json"),
    )
    sql_failure = _run(p1_2_store, "SELECT * FROM main.transactions")
    serialized = json.dumps([llm_failure.trace.as_dict(), sql_failure.trace.as_dict()])

    assert llm_failure.error and llm_failure.error.code == "invalid_json"
    assert llm_failure.trace.llm is not None
    assert sql_failure.error and sql_failure.error.code == "sql_execution_error"
    assert str(p1_2_store.operational_path) not in serialized
    assert str(p1_2_store.private_path) not in serialized


def test_private_database_is_rejected_without_exposing_private_contents(p1_2_store) -> None:
    with sqlite3.connect(p1_2_store.private_path) as private_db:
        private_db.execute("INSERT INTO private_episodes VALUES ('ep-a')")
        private_db.execute(
            "INSERT INTO episode_manifest VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("ep-a", "development", 1, "clean", "a" * 64, "b" * 64, "{}"),
        )
        private_db.execute(
            "INSERT INTO incident_truth VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "truth-secret",
                "ep-a",
                "hidden-cause-secret",
                1,
                "hidden-mechanism-secret",
                0,
                "[]",
                "[]",
                "[]",
                "2026-02-01T00:00:00.000000Z",
                None,
                "2026-02-01T00:00:00.000000Z",
                "2026-02-01T00:00:00.000000Z",
                "2026-02-08T00:00:00.000000Z",
                "[]",
                None,
            ),
        )
    result = run_sql(
        "Show all data",
        "ep-a",
        AS_OF,
        operational_db=p1_2_store.private_path,
        client=FakeLLMClient(response={"sql": "SELECT * FROM incident_truth"}),
    )

    serialized = json.dumps(result.trace.as_dict())
    assert result.error and result.error.code == "operational_db_invalid"
    assert "hidden-cause-secret" not in serialized
    assert "hidden-mechanism-secret" not in serialized
    assert str(p1_2_store.private_path) not in serialized
    assert result.trace.llm is None


def test_result_row_limit_is_enforced(p1_2_store) -> None:
    config = FakeLLMClient().config
    from dataclasses import replace

    config = replace(config, max_result_rows=2)
    result = run_sql(
        "List transactions",
        "ep-a",
        AS_OF,
        operational_db=p1_2_store.operational_path,
        client=FakeLLMClient(response={"sql": "SELECT * FROM asof.transactions"}, config=config),
    )

    assert result.ok
    assert result.trace.row_count == 2
    assert result.trace.truncated


def test_sql_execution_time_limit_returns_structured_error(p1_2_store) -> None:
    from dataclasses import replace

    config = replace(FakeLLMClient().config, sql_timeout_seconds=0.001)
    result = run_sql(
        "Sum a long recursive sequence",
        "ep-a",
        AS_OF,
        operational_db=p1_2_store.operational_path,
        client=FakeLLMClient(
            response={
                "sql": (
                    "WITH RECURSIVE numbers(x) AS (VALUES(1) UNION ALL "
                    "SELECT x + 1 FROM numbers WHERE x < 100000000) "
                    "SELECT sum(x) FROM numbers"
                )
            },
            config=config,
        ),
    )

    assert result.error and result.error.code == "sql_timeout"


def test_single_expensive_sql_function_cannot_escape_deadline(p1_2_store) -> None:
    from dataclasses import replace

    config = replace(FakeLLMClient().config, sql_timeout_seconds=0.001)
    result = run_sql(
        "Return the size of a generated value",
        "ep-a",
        AS_OF,
        operational_db=p1_2_store.operational_path,
        client=FakeLLMClient(
            response={"sql": "SELECT length(hex(zeroblob(5000000))) AS n"},
            config=config,
        ),
    )

    assert result.error and result.error.code == "sql_timeout"


def test_result_byte_limit_returns_structured_error(p1_2_store) -> None:
    from dataclasses import replace

    config = replace(FakeLLMClient().config, max_result_bytes=64)
    result = run_sql(
        "Return a long value",
        "ep-a",
        AS_OF,
        operational_db=p1_2_store.operational_path,
        client=FakeLLMClient(
            response={"sql": "SELECT printf('%0100d', 1) AS value"},
            config=config,
        ),
    )

    assert result.error and result.error.code == "sql_result_too_large"


def test_blob_result_returns_structured_error_and_keeps_sql_trace(p1_2_store) -> None:
    blob_result = _run(p1_2_store, "SELECT x'41' AS value", question="Return a binary value")
    hex_result = _run(p1_2_store, "SELECT hex(x'41') AS value", question="Return a hex value")

    assert not blob_result.ok
    assert blob_result.error and blob_result.error.code == "sql_unsupported_type"
    assert blob_result.rows == []
    assert blob_result.trace.sql == "SELECT x'41' AS value"
    assert blob_result.trace.llm is not None
    assert hex_result.ok
    assert hex_result.rows == [{"value": "41"}]


def test_duplicate_aliases_never_overwrite_an_existing_alias(p1_2_store) -> None:
    result = _run(
        p1_2_store,
        "SELECT 1 AS x, 2 AS x, 3 AS x_2",
        question="Preserve all output columns",
    )

    assert result.ok
    assert result.rows == [{"x": 1, "x_3": 2, "x_2": 3}]


_TRANSACTIONS_AS_OF_SQL_CASES = (
    "SELECT txn_id FROM asof.transactions WHERE amount = '125000' ORDER BY txn_id",
    "SELECT txn_id FROM asof.transactions WHERE amount IN ('125000', '175000') ORDER BY txn_id",
    "SELECT txn_id FROM asof.transactions WHERE amount > '150000' ORDER BY txn_id",
    "SELECT COUNT(*) AS n FROM asof.transactions WHERE channel = 'qr'",
)


def _same_sql_on_asof_source_rows(operational_path: Path, sql: str) -> list[tuple]:
    """Answer ``sql`` over as-of views of the source tables, keeping their declared affinity."""
    reference = sqlite3.connect(":memory:", uri=True)
    try:
        reference.execute(
            "ATTACH DATABASE ? AS src",
            (f"{operational_path.resolve().as_uri()}?mode=ro",),
        )
        reference.execute("ATTACH DATABASE ':memory:' AS asof")
        # CREATE TABLE ... AS SELECT keeps each column's affinity from the source column.
        reference.execute(
            "CREATE TABLE asof.transactions AS SELECT * FROM src.transactions "
            "WHERE available_at <= '2026-02-02T00:00:00.000000Z' "
            "AND ((episode_id = 'base' AND event_at < '2026-01-31T17:00:00.000000Z') "
            "OR episode_id = 'ep-a') ORDER BY event_at, rowid"
        )
        return reference.execute(sql).fetchall()
    finally:
        reference.close()


def test_sql_through_run_sql_returns_the_rows_the_same_sql_returns_on_the_asof_source(
    p1_2_store,
) -> None:
    # Declared affinity matters: the source stores amount as INTEGER, so the text literal
    # '125000' matches it. A snapshot that stores every column as BLOB returns nothing.
    for sql in _TRANSACTIONS_AS_OF_SQL_CASES:
        expected = _same_sql_on_asof_source_rows(p1_2_store.operational_path, sql)
        result = _run(p1_2_store, sql)

        assert result.ok, (sql, result.error)
        assert [tuple(row.values()) for row in result.rows] == expected, sql
    assert _same_sql_on_asof_source_rows(
        p1_2_store.operational_path, _TRANSACTIONS_AS_OF_SQL_CASES[0]
    ) == [("txn-base-before-t0",)]


def test_snapshot_cache_builds_once_per_cutoff_until_a_commit_changes_the_data(p1_2_store) -> None:
    listing = "SELECT txn_id FROM asof.transactions ORDER BY txn_id"
    path = p1_2_store.operational_path
    cache = SnapshotCache()

    def ask(question: str, *, snapshot_cache: SnapshotCache | None = None):
        # FakeLLMClient serves one response per instance, so each question gets its own client.
        return run_sql(
            question,
            "ep-a",
            AS_OF,
            operational_db=path,
            client=FakeLLMClient(response={"sql": listing}),
            snapshot_cache=snapshot_cache,
        )

    first = ask("List visible transactions", snapshot_cache=cache)
    second = ask("List visible transactions again", snapshot_cache=cache)
    uncached = ask("List visible transactions")

    assert uncached.ok, uncached.error
    assert (cache.builds, cache.reuses) == (1, 1)
    assert first.rows == second.rows == uncached.rows
    assert first.trace.data_snapshot == second.trace.data_snapshot == uncached.trace.data_snapshot

    insert_operational_record(
        p1_2_store.operational,
        "transactions",
        {
            "txn_id": "txn-a-committed-later",
            "episode_id": "ep-a",
            "customer_id": "customer-base",
            "merchant_id": "merchant-base",
            "created_at": "2026-02-01T12:00:00.000000Z",
            "event_at": "2026-02-01T12:00:00.000000Z",
            "available_at": "2026-02-01T12:00:00.000000Z",
            "amount": 333000,
            "channel": "qr",
        },
    )
    p1_2_store.operational.commit()

    after_commit = ask("List visible transactions", snapshot_cache=cache)

    assert (cache.builds, cache.reuses) == (2, 1)
    assert "txn-a-committed-later" in {row["txn_id"] for row in after_commit.rows}
    assert after_commit.trace.data_snapshot != first.trace.data_snapshot


@pytest.mark.parametrize(
    ("injected", "expected_code"),
    [
        (sqlite3.OperationalError("unable to open database file: /var/secret/leak.sqlite"), "snapshot_source_unavailable"),
        (StorageError("episode is not registered: '/var/secret/leak'"), "snapshot_scope_invalid"),
        (RuntimeError("unexpected state at /var/secret/leak"), "snapshot_internal_error"),
    ],
)
def test_snapshot_build_failures_are_classified_without_exposing_details(
    p1_2_store, monkeypatch, injected: Exception, expected_code: str
) -> None:
    def fail_on_read(connection, table, **kwargs):
        raise injected

    monkeypatch.setattr("triage.tools.read_as_of", fail_on_read)
    result = _run(p1_2_store, "SELECT 1 AS one")
    serialized = json.dumps(result.trace.as_dict())

    assert result.error and result.error.code == expected_code
    assert result.rows == []
    assert result.trace.llm is None
    assert "/var/secret/leak" not in serialized
    assert str(p1_2_store.operational_path) not in serialized
