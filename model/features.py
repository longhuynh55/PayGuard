"""Point-in-time transaction features built with in-memory DuckDB."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

import duckdb
import sqlite3

from model.data import ModelExample, ModelSplits
from simulator.asof import read_as_of


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

_TRANSACTION_COLUMNS = (
    "txn_id",
    "episode_id",
    "customer_id",
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
    if transactions:
        connection.executemany(
            "INSERT INTO source_transactions VALUES (?, ?, ?, ?, ?, ?)",
            [_selected_columns(row, _TRANSACTION_COLUMNS) for row in transactions],
        )
    if customers:
        connection.executemany(
            "INSERT INTO source_customers VALUES (?, ?, ?, ?)",
            [_selected_columns(row, _CUSTOMER_COLUMNS) for row in customers],
        )


_FEATURE_QUERY = """
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
          AND h.created_at >= e.created_at - INTERVAL '24 hours'
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
          AND h.created_at >= e.created_at - INTERVAL '7 days'
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
            connection.executemany(
                "INSERT INTO feature_examples VALUES (?, ?, ?, ?, ?)",
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
