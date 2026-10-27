from __future__ import annotations

import json

import sqlite3

from triage.llm import FakeLLMClient
from triage.tools import run_sql


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


def test_duplicate_aliases_never_overwrite_an_existing_alias(p1_2_store) -> None:
    result = _run(
        p1_2_store,
        "SELECT 1 AS x, 2 AS x, 3 AS x_2",
        question="Preserve all output columns",
    )

    assert result.ok
    assert result.rows == [{"x": 1, "x_3": 2, "x_2": 3}]
