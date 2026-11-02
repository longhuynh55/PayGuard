"""Point-in-time transaction features built with in-memory DuckDB."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

import duckdb
import sqlite3

from model.data import ModelExample, ModelSplits
from simulator.asof import read_as_of
from simulator.storage import utc_timestamp


FeatureValue: TypeAlias = int | float | None

FEATURE_NAMES = (
    "txn_count_1h",
    "txn_count_24h",
    "txn_count_7d",
    "amount_sum_7d",
    "amount_avg_7d",
    "amount_to_customer_historical_avg",
    "history_window_observed_1h",
    "history_window_observed_24h",
    "history_window_observed_7d",
)

# Public lookbacks used by monitoring and the evaluator. Model features mirror
# the SQL below; `txn_count` is the one-business-day P3.7 volume metric from
# docs/data.md.
FEATURE_LOOKBACK_DAYS = {
    "txn_count_24h": 1,
    "amount_avg_7d": 7,
    "txn_count": 1,
}

_TRANSACTION_COLUMNS = (
    "txn_id",
    "episode_id",
    "customer_id",
    "device_id",
    "card_token",
    "created_at",
    "available_at",
    "amount",
)
_CUSTOMER_COLUMNS = ("customer_id", "episode_id", "event_at", "available_at")


@dataclass(frozen=True)
class FeatureMatrix:
    """A deterministic feature matrix aligned with its model examples."""

    examples: tuple[ModelExample, ...]
    feature_names: tuple[str, ...]
    values: tuple[tuple[FeatureValue, ...], ...]

    @property
    def txn_ids(self) -> tuple[str, ...]:
        return tuple(example.txn_id for example in self.examples)

    @property
    def targets(self) -> tuple[int, ...]:
        return tuple(example.target for example in self.examples)


@dataclass(frozen=True)
class FeatureSplits:
    """Fit, validation, and calibration feature matrices for one episode."""

    episode_id: str
    as_of_time: str
    feature_names: tuple[str, ...]
    fit: FeatureMatrix
    validation: FeatureMatrix
    calibration: FeatureMatrix


@dataclass(frozen=True)
class ScoreExample:
    """One unlabeled transaction to score at its decision timestamp."""

    txn_id: str
    episode_id: str
    event_at: str


@dataclass(frozen=True)
class ScoreFeatureMatrix:
    """Point-in-time features aligned with monitoring transactions."""

    examples: tuple[ScoreExample, ...]
    feature_names: tuple[str, ...]
    values: tuple[tuple[FeatureValue, ...], ...]


class FeatureAsOfError(ValueError):
    """Raised when a split example cannot be observed at its decision time."""


def _selected_columns(row: dict[str, object], columns: tuple[str, ...]) -> tuple[object, ...]:
    """Copy only operational columns required by the feature SQL."""
    try:
        return tuple(row[column] for column in columns)
    except KeyError as exc:
        raise FeatureAsOfError(f"as-of row is missing required column {exc.args[0]!r}") from exc


def _create_source_tables(
    connection: duckdb.DuckDBPyConnection,
    transactions: list[dict[str, object]],
    customers: list[dict[str, object]],
) -> None:
    connection.execute(
        """
        CREATE TABLE source_transactions (
            txn_id VARCHAR NOT NULL,
            episode_id VARCHAR NOT NULL,
            customer_id VARCHAR NOT NULL,
            device_id VARCHAR,
            card_token VARCHAR,
            created_at TIMESTAMPTZ NOT NULL,
            available_at TIMESTAMPTZ NOT NULL,
            amount BIGINT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE source_customers (
            customer_id VARCHAR NOT NULL,
            episode_id VARCHAR NOT NULL,
            event_at TIMESTAMPTZ NOT NULL,
            available_at TIMESTAMPTZ NOT NULL
        )
        """
    )
    _bulk_insert(
        connection,
        "source_transactions",
        _SOURCE_TRANSACTION_TYPES,
        [_selected_columns(row, _TRANSACTION_COLUMNS) for row in transactions],
    )
    _bulk_insert(
        connection,
        "source_customers",
        _SOURCE_CUSTOMER_TYPES,
        [_selected_columns(row, _CUSTOMER_COLUMNS) for row in customers],
    )


_SOURCE_TRANSACTION_TYPES = (
    ("txn_id", "VARCHAR"),
    ("episode_id", "VARCHAR"),
    ("customer_id", "VARCHAR"),
    ("device_id", "VARCHAR"),
    ("card_token", "VARCHAR"),
    ("created_at", "TIMESTAMPTZ"),
    ("available_at", "TIMESTAMPTZ"),
    ("amount", "BIGINT"),
)
_SOURCE_CUSTOMER_TYPES = (
    ("customer_id", "VARCHAR"),
    ("episode_id", "VARCHAR"),
    ("event_at", "TIMESTAMPTZ"),
    ("available_at", "TIMESTAMPTZ"),
)
_FEATURE_EXAMPLE_TYPES = (
    ("position", "BIGINT"),
    ("txn_id", "VARCHAR"),
    ("transaction_episode_id", "VARCHAR"),
    ("episode_id", "VARCHAR"),
    ("created_at", "TIMESTAMPTZ"),
)


def _bulk_insert(
    connection: duckdb.DuckDBPyConnection,
    table: str,
    column_types: tuple[tuple[str, str], ...],
    rows: list[tuple[object, ...]],
) -> None:
    """Insert rows with one typed array bind per column.

    ``executemany`` costs one client round trip per row (about 45 s for 43k
    rows); a single ``unnest`` insert binds each column once. Values are bound
    as VARCHAR/BIGINT and cast in SQL, which yields the same typed values.
    """
    if not rows:
        return
    names = ", ".join(name for name, _ in column_types)
    selects = ", ".join(
        f"CAST(unnest(CAST(? AS {'BIGINT' if sql_type == 'BIGINT' else 'VARCHAR'}[])) "
        f"AS {sql_type})"
        for _, sql_type in column_types
    )
    params = [list(column) for column in zip(*rows, strict=True)]
    connection.execute(f"INSERT INTO {table} ({names}) SELECT {selects}", params)


_FEATURE_QUERY = f"""
SELECT
    e.position,
    target.txn_id IS NOT NULL AS target_available,
    (
        SELECT COUNT(*)
        FROM source_transactions AS h
        WHERE h.customer_id = target.customer_id
          AND h.episode_id IN ('base', e.episode_id)
          AND h.available_at <= e.created_at
          AND h.created_at >= e.created_at - INTERVAL '1 hour'
          AND h.created_at < e.created_at
          AND h.txn_id <> e.txn_id
    ) AS txn_count_1h,
    (
        SELECT COUNT(*)
        FROM source_transactions AS h
        WHERE h.customer_id = target.customer_id
          AND h.episode_id IN ('base', e.episode_id)
          AND h.available_at <= e.created_at
          AND h.created_at >= e.created_at - INTERVAL '{FEATURE_LOOKBACK_DAYS['txn_count_24h'] * 24} hours'
          AND h.created_at < e.created_at
          AND h.txn_id <> e.txn_id
    ) AS txn_count_24h,
    (
        SELECT COUNT(*)
        FROM source_transactions AS h
        WHERE h.customer_id = target.customer_id
          AND h.episode_id IN ('base', e.episode_id)
          AND h.available_at <= e.created_at
          AND h.created_at >= e.created_at - INTERVAL '7 days'
          AND h.created_at < e.created_at
          AND h.txn_id <> e.txn_id
    ) AS txn_count_7d,
    (
        SELECT SUM(h.amount)
        FROM source_transactions AS h
        WHERE h.customer_id = target.customer_id
          AND h.episode_id IN ('base', e.episode_id)
          AND h.available_at <= e.created_at
          AND h.created_at >= e.created_at - INTERVAL '7 days'
          AND h.created_at < e.created_at
          AND h.txn_id <> e.txn_id
    ) AS amount_sum_7d,
    (
        SELECT AVG(h.amount)
        FROM source_transactions AS h
        WHERE h.customer_id = target.customer_id
          AND h.episode_id IN ('base', e.episode_id)
          AND h.available_at <= e.created_at
          AND h.created_at >= e.created_at - INTERVAL '{FEATURE_LOOKBACK_DAYS['amount_avg_7d']} days'
          AND h.created_at < e.created_at
          AND h.txn_id <> e.txn_id
    ) AS amount_avg_7d,
    CASE
        WHEN target.amount IS NULL OR history.avg_amount IS NULL OR history.avg_amount = 0
        THEN NULL
        ELSE CAST(target.amount AS DOUBLE) / history.avg_amount
    END AS amount_to_customer_historical_avg,
    CASE
        WHEN customer.customer_id IS NOT NULL
         AND GREATEST(customer.event_at, customer.available_at)
             <= e.created_at - INTERVAL '1 hour'
        THEN 1 ELSE 0
    END AS history_window_observed_1h,
    CASE
        WHEN customer.customer_id IS NOT NULL
         AND GREATEST(customer.event_at, customer.available_at)
             <= e.created_at - INTERVAL '24 hours'
        THEN 1 ELSE 0
    END AS history_window_observed_24h,
    CASE
        WHEN customer.customer_id IS NOT NULL
         AND GREATEST(customer.event_at, customer.available_at)
             <= e.created_at - INTERVAL '7 days'
        THEN 1 ELSE 0
    END AS history_window_observed_7d
FROM feature_examples AS e
LEFT JOIN source_transactions AS target
  ON target.txn_id = e.txn_id
 AND target.episode_id = e.transaction_episode_id
 AND target.created_at = e.created_at
 AND target.available_at <= e.created_at
LEFT JOIN source_customers AS customer
  ON customer.customer_id = target.customer_id
 AND customer.episode_id IN ('base', e.episode_id)
 AND customer.available_at <= e.created_at
LEFT JOIN LATERAL (
    SELECT AVG(h.amount) AS avg_amount
    FROM source_transactions AS h
    WHERE h.customer_id = target.customer_id
      AND h.episode_id IN ('base', e.episode_id)
      AND h.available_at <= e.created_at
      AND h.created_at < e.created_at
      AND h.txn_id <> e.txn_id
) AS history ON TRUE
ORDER BY e.position
"""


def _matrix(
    examples: tuple[ModelExample, ...],
    rows_by_txn_id: dict[str, tuple[FeatureValue, ...]],
) -> FeatureMatrix:
    return FeatureMatrix(
        examples=examples,
        feature_names=FEATURE_NAMES,
        values=tuple(rows_by_txn_id[example.txn_id] for example in examples),
    )


def build_feature_splits(
    operational: sqlite3.Connection,
    splits: ModelSplits,
) -> FeatureSplits:
    """Build feature matrices using only point-in-time operational snapshots.

    The snapshot is read through the simulator's sole as-of API. DuckDB then
    applies the per-example decision-time availability cutoff and excludes the
    target transaction from its own history.
    """
    all_examples = splits.fit + splits.validation + splits.calibration
    if any(example.episode_id not in {"base", splits.episode_id} for example in all_examples):
        raise FeatureAsOfError("split examples must belong to base or the split episode_id")

    transactions = read_as_of(
        operational,
        "transactions",
        episode_id=splits.episode_id,
        t0=splits.as_of_time,
        as_of_time=splits.as_of_time,
    )
    customers = read_as_of(
        operational,
        "customers",
        episode_id=splits.episode_id,
        t0=splits.as_of_time,
        as_of_time=splits.as_of_time,
    )

    with duckdb.connect(database=":memory:") as connection:
        connection.execute("SET threads = 1")
        _create_source_tables(connection, transactions, customers)
        connection.execute(
            """
            CREATE TABLE feature_examples (
                position BIGINT NOT NULL,
                txn_id VARCHAR NOT NULL,
                transaction_episode_id VARCHAR NOT NULL,
                episode_id VARCHAR NOT NULL,
                created_at TIMESTAMPTZ NOT NULL
            )
            """
        )
        if all_examples:
            _bulk_insert(
                connection,
                "feature_examples",
                _FEATURE_EXAMPLE_TYPES,
                [
                    (
                        index,
                        example.txn_id,
                        example.episode_id,
                        splits.episode_id,
                        example.event_at,
                    )
                    for index, example in enumerate(all_examples)
                ],
            )
            raw_rows = connection.execute(_FEATURE_QUERY).fetchall()
        else:
            raw_rows = []

    rows_by_txn_id: dict[str, tuple[FeatureValue, ...]] = {}
    for raw_row in raw_rows:
        position, target_available, *values = raw_row
        example = all_examples[position]
        if not target_available:
            raise FeatureAsOfError(
                f"transaction {example.txn_id!r} is not available at its decision time "
                f"{example.event_at}"
            )
        if example.txn_id in rows_by_txn_id:
            raise FeatureAsOfError(f"duplicate split transaction ID {example.txn_id!r}")
        rows_by_txn_id[example.txn_id] = tuple(
            None
            if value is None
            else int(value)
            if index in {0, 1, 2, 3, 6, 7, 8}
            else float(value)
            for index, value in enumerate(values)
        )

    if set(rows_by_txn_id) != {example.txn_id for example in all_examples}:
        raise FeatureAsOfError("feature SQL did not return one row for every split example")

    return FeatureSplits(
        episode_id=splits.episode_id,
        as_of_time=splits.as_of_time,
        feature_names=FEATURE_NAMES,
        fit=_matrix(splits.fit, rows_by_txn_id),
        validation=_matrix(splits.validation, rows_by_txn_id),
        calibration=_matrix(splits.calibration, rows_by_txn_id),
    )


def build_score_feature_matrix(
    operational: sqlite3.Connection,
    transactions: tuple[dict[str, object], ...] | list[dict[str, object]],
    *,
    episode_id: str,
    t0: str,
    include_base_targets: bool = False,
) -> ScoreFeatureMatrix:
    """Build monitoring features from a separate as-of snapshot per decision.

    The input rows only identify the transactions to score. For each distinct
    decision timestamp, both feature sources are fetched again through
    ``read_as_of`` at that exact timestamp and episode. ``include_base_targets``
    is for fixed-reference feature distributions; it reuses the same P2.2 SQL
    and observation flags for base transactions without changing score callers.
    """
    examples: list[ScoreExample] = []
    seen: set[str] = set()
    transaction_episode_by_txn_id: dict[str, str] = {}
    for transaction in transactions:
        txn_id = transaction.get("txn_id")
        row_episode_id = transaction.get("episode_id")
        decision_at = transaction.get("created_at")
        available_at = transaction.get("available_at")
        if not all(
            isinstance(value, str) and value
            for value in (txn_id, row_episode_id, decision_at, available_at)
        ):
            raise FeatureAsOfError("score transaction is missing its ID, episode, or decision time")
        if row_episode_id != episode_id and not (
            include_base_targets and row_episode_id == "base"
        ):
            raise FeatureAsOfError("score transactions must belong to the requested episode")
        if transaction.get("event_at") != decision_at:
            raise FeatureAsOfError("score transaction event_at must equal created_at")
        if available_at > decision_at:
            raise FeatureAsOfError("score transaction is not available at its decision time")
        if txn_id in seen:
            raise FeatureAsOfError(f"duplicate score transaction ID {txn_id!r}")
        seen.add(txn_id)
        transaction_episode_by_txn_id[txn_id] = str(row_episode_id)
        examples.append(ScoreExample(txn_id, episode_id, decision_at))

    examples.sort(key=lambda example: (example.event_at, example.txn_id))
    by_decision: dict[str, list[ScoreExample]] = {}
    for example in examples:
        by_decision.setdefault(example.event_at, []).append(example)

    # Rows visible at any decision are a subset of rows visible at the latest
    # decision (``available_at <= as_of`` is monotone and the episode/base scope
    # is identical), so one as-of read at the latest decision time yields the
    # same row set as the union of per-decision snapshots. The SQL below still
    # applies each example's own ``available_at <= created_at`` bound.
    snapshot_transactions: dict[str, dict[str, object]] = {}
    snapshot_customers: dict[str, dict[str, object]] = {}
    if by_decision:
        latest_decision_at = max(by_decision, key=utc_timestamp)
        transactions_as_of = read_as_of(
            operational,
            "transactions",
            episode_id=episode_id,
            t0=t0,
            as_of_time=latest_decision_at,
        )
        customers_as_of = read_as_of(
            operational,
            "customers",
            episode_id=episode_id,
            t0=t0,
            as_of_time=latest_decision_at,
        )
        _merge_as_of_rows(snapshot_transactions, transactions_as_of, "txn_id")
        _merge_as_of_rows(snapshot_customers, customers_as_of, "customer_id")

    values_by_txn_id: dict[str, tuple[FeatureValue, ...]] = {}
    all_examples = tuple(examples)
    with duckdb.connect(database=":memory:") as connection:
        connection.execute("SET threads = 1")
        _create_source_tables(
            connection,
            list(snapshot_transactions.values()),
            list(snapshot_customers.values()),
        )
        connection.execute(
            """
            CREATE TABLE feature_examples (
                position BIGINT NOT NULL,
                txn_id VARCHAR NOT NULL,
                transaction_episode_id VARCHAR NOT NULL,
                episode_id VARCHAR NOT NULL,
                created_at TIMESTAMPTZ NOT NULL
            )
            """
        )
        if all_examples:
            _bulk_insert(
                connection,
                "feature_examples",
                _FEATURE_EXAMPLE_TYPES,
                [
                    (
                        index,
                        example.txn_id,
                        transaction_episode_by_txn_id[example.txn_id],
                        episode_id,
                        example.event_at,
                    )
                    for index, example in enumerate(all_examples)
                ],
            )
            raw_rows = connection.execute(_FEATURE_QUERY).fetchall()
        else:
            raw_rows = []

    for raw_row in raw_rows:
        position, target_available, *values = raw_row
        example = all_examples[position]
        if not target_available:
            raise FeatureAsOfError(
                f"transaction {example.txn_id!r} is not available at its decision time "
                f"{example.event_at}"
            )
        if example.txn_id in values_by_txn_id:
            raise FeatureAsOfError(f"duplicate score transaction ID {example.txn_id!r}")
        values_by_txn_id[example.txn_id] = tuple(
            None
            if value is None
            else int(value)
            if index in {0, 1, 2, 3, 6, 7, 8}
            else float(value)
            for index, value in enumerate(values)
        )

    if set(values_by_txn_id) != {example.txn_id for example in examples}:
        raise FeatureAsOfError("feature SQL did not return one row for every score transaction")
    return ScoreFeatureMatrix(
        examples=tuple(examples),
        feature_names=FEATURE_NAMES,
        values=tuple(values_by_txn_id[example.txn_id] for example in examples),
    )


def _merge_as_of_rows(
    destination: dict[str, dict[str, object]],
    rows: list[dict[str, object]],
    key_name: str,
) -> None:
    for row in rows:
        key = row[key_name]
        existing = destination.get(key)
        if existing is not None and existing != row:
            raise FeatureAsOfError(
                f"operational row {key!r} changed between as-of snapshots"
            )
        destination[key] = row
