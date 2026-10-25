"""SQLite schemas for operational observations and private evaluator data."""

from __future__ import annotations

from collections.abc import Iterable


UTC_TEXT_CHECK = (
    "{column} GLOB "
    "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T"
    "[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'"
)

OPERATIONAL_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS episodes (
        episode_id TEXT NOT NULL PRIMARY KEY CHECK (length(episode_id) > 0),
        role TEXT NOT NULL CHECK (role IN ('base', 'development', 'calibration', 'test')),
        CHECK ((episode_id = 'base') = (role = 'base'))
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS customers (
        customer_id TEXT NOT NULL PRIMARY KEY CHECK (length(customer_id) > 0),
        episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
        event_at TEXT NOT NULL CHECK ({UTC_TEXT_CHECK.format(column='event_at')}),
        available_at TEXT NOT NULL CHECK ({UTC_TEXT_CHECK.format(column='available_at')}),
        segment TEXT NOT NULL CHECK (length(segment) > 0),
        province TEXT NOT NULL CHECK (length(province) > 0),
        age_band TEXT NULL
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS merchants (
        merchant_id TEXT NOT NULL PRIMARY KEY CHECK (length(merchant_id) > 0),
        episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
        event_at TEXT NOT NULL CHECK ({UTC_TEXT_CHECK.format(column='event_at')}),
        available_at TEXT NOT NULL CHECK ({UTC_TEXT_CHECK.format(column='available_at')}),
        mcc TEXT NOT NULL CHECK (length(mcc) > 0),
        province TEXT NOT NULL CHECK (length(province) > 0)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS transactions (
        txn_id TEXT NOT NULL PRIMARY KEY CHECK (length(txn_id) > 0),
        episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
        customer_id TEXT NOT NULL REFERENCES customers(customer_id),
        merchant_id TEXT NOT NULL REFERENCES merchants(merchant_id),
        created_at TEXT NOT NULL CHECK ({UTC_TEXT_CHECK.format(column='created_at')}),
        event_at TEXT NOT NULL CHECK ({UTC_TEXT_CHECK.format(column='event_at')}),
        available_at TEXT NOT NULL CHECK ({UTC_TEXT_CHECK.format(column='available_at')}),
        amount INTEGER NULL CHECK (amount IS NULL OR typeof(amount) = 'integer'),
        channel TEXT NOT NULL CHECK (length(channel) > 0),
        UNIQUE (txn_id, episode_id),
        CHECK (event_at = created_at)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS transaction_events (
        event_id TEXT NOT NULL PRIMARY KEY CHECK (length(event_id) > 0),
        txn_id TEXT NOT NULL,
        episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
        event_type TEXT NOT NULL CHECK (
            event_type IN ('authorized', 'settled', 'declined', 'refunded', 'reversed')
        ),
        event_at TEXT NOT NULL CHECK ({UTC_TEXT_CHECK.format(column='event_at')}),
        available_at TEXT NOT NULL CHECK ({UTC_TEXT_CHECK.format(column='available_at')}),
        response_code TEXT NULL,
        FOREIGN KEY (txn_id, episode_id) REFERENCES transactions(txn_id, episode_id),
        CHECK (available_at >= event_at)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS label_events (
        event_id TEXT NOT NULL PRIMARY KEY CHECK (length(event_id) > 0),
        txn_id TEXT NOT NULL,
        episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
        event_at TEXT NOT NULL CHECK ({UTC_TEXT_CHECK.format(column='event_at')}),
        label TEXT NOT NULL CHECK (label IN ('fraud', 'legit')),
        label_status TEXT NOT NULL CHECK (label_status = 'confirmed'),
        reported_at TEXT NOT NULL CHECK ({UTC_TEXT_CHECK.format(column='reported_at')}),
        available_at TEXT NOT NULL CHECK ({UTC_TEXT_CHECK.format(column='available_at')}),
        source TEXT NOT NULL CHECK (source IN ('chargeback', 'investigator', 'customer_report')),
        supersedes_event_id TEXT NULL REFERENCES label_events(event_id),
        UNIQUE (txn_id),
        FOREIGN KEY (txn_id, episode_id) REFERENCES transactions(txn_id, episode_id),
        CHECK (available_at >= reported_at),
        CHECK (supersedes_event_id IS NULL)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS business_calendar (
        calendar_event_id TEXT NOT NULL PRIMARY KEY CHECK (length(calendar_event_id) > 0),
        episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
        event_at TEXT NOT NULL CHECK ({UTC_TEXT_CHECK.format(column='event_at')}),
        available_at TEXT NOT NULL CHECK ({UTC_TEXT_CHECK.format(column='available_at')}),
        business_date TEXT NOT NULL CHECK (
            length(business_date) = 10 AND business_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        ),
        event_name TEXT NOT NULL CHECK (length(event_name) > 0),
        lag_days INTEGER NOT NULL CHECK (typeof(lag_days) = 'integer'),
        expected_effect_json TEXT NOT NULL CHECK (json_valid(expected_effect_json)),
        scope_json TEXT NOT NULL CHECK (json_valid(scope_json)),
        CHECK (json_type(expected_effect_json) = 'object'),
        CHECK (json_type(scope_json) = 'object')
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS transactions_customer_episode_insert
    BEFORE INSERT ON transactions
    WHEN (SELECT episode_id FROM customers WHERE customer_id = NEW.customer_id)
         NOT IN ('base', NEW.episode_id)
    BEGIN
        SELECT RAISE(ABORT, 'transaction customer must be base or same episode');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS transactions_merchant_episode_insert
    BEFORE INSERT ON transactions
    WHEN (SELECT episode_id FROM merchants WHERE merchant_id = NEW.merchant_id)
         NOT IN ('base', NEW.episode_id)
    BEGIN
        SELECT RAISE(ABORT, 'transaction merchant must be base or same episode');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS transactions_identity_immutable
    BEFORE UPDATE OF txn_id, episode_id, customer_id, merchant_id ON transactions
    WHEN NEW.txn_id IS NOT OLD.txn_id
      OR NEW.episode_id IS NOT OLD.episode_id
      OR NEW.customer_id IS NOT OLD.customer_id
      OR NEW.merchant_id IS NOT OLD.merchant_id
    BEGIN
        SELECT RAISE(ABORT, 'transaction identity and episode relationships are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS customers_identity_immutable
    BEFORE UPDATE OF customer_id, episode_id ON customers
    WHEN NEW.customer_id IS NOT OLD.customer_id OR NEW.episode_id IS NOT OLD.episode_id
    BEGIN
        SELECT RAISE(ABORT, 'customer identity and episode are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS merchants_identity_immutable
    BEFORE UPDATE OF merchant_id, episode_id ON merchants
    WHEN NEW.merchant_id IS NOT OLD.merchant_id OR NEW.episode_id IS NOT OLD.episode_id
    BEGIN
        SELECT RAISE(ABORT, 'merchant identity and episode are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS transaction_events_no_update
    BEFORE UPDATE ON transaction_events
    BEGIN
        SELECT RAISE(ABORT, 'transaction_events is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS transaction_events_no_replace
    BEFORE INSERT ON transaction_events
    WHEN EXISTS (SELECT 1 FROM transaction_events WHERE event_id = NEW.event_id)
    BEGIN
        SELECT RAISE(ABORT, 'transaction_events is append-only; event ids cannot be replaced');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS transaction_events_no_delete
    BEFORE DELETE ON transaction_events
    BEGIN
        SELECT RAISE(ABORT, 'transaction_events is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS label_event_time_matches_transaction
    BEFORE INSERT ON label_events
    WHEN (SELECT event_at FROM transactions
          WHERE txn_id = NEW.txn_id AND episode_id = NEW.episode_id) IS NOT NULL
     AND (SELECT event_at FROM transactions
          WHERE txn_id = NEW.txn_id AND episode_id = NEW.episode_id) <> NEW.event_at
    BEGIN
        SELECT RAISE(ABORT, 'label event_at must match its transaction event_at');
    END
    """,
)

PRIVATE_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS private_episodes (
        episode_id TEXT NOT NULL PRIMARY KEY CHECK (length(episode_id) > 0)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS run_metadata (
        key TEXT NOT NULL PRIMARY KEY CHECK (length(key) > 0),
        value_json TEXT NOT NULL CHECK (json_valid(value_json))
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS incident_truth (
        incident_truth_id TEXT NOT NULL PRIMARY KEY CHECK (length(incident_truth_id) > 0),
        episode_id TEXT NOT NULL REFERENCES private_episodes(episode_id),
        cause TEXT NOT NULL CHECK (length(cause) > 0),
        requires_intervention INTEGER NOT NULL CHECK (requires_intervention IN (0, 1)),
        mechanism TEXT NOT NULL CHECK (length(mechanism) > 0),
        rule_covered INTEGER NOT NULL CHECK (rule_covered IN (0, 1)),
        affected_entities_json TEXT NOT NULL CHECK (json_valid(affected_entities_json)),
        affected_segments_json TEXT NOT NULL CHECK (json_valid(affected_segments_json)),
        affected_features_json TEXT NOT NULL CHECK (json_valid(affected_features_json)),
        effect_start_at TEXT NOT NULL CHECK ({UTC_TEXT_CHECK.format(column='effect_start_at')}),
        effect_end_at TEXT NULL CHECK (
            effect_end_at IS NULL OR ({UTC_TEXT_CHECK.format(column='effect_end_at')})
        ),
        effect_available_at TEXT NOT NULL CHECK ({UTC_TEXT_CHECK.format(column='effect_available_at')}),
        intervention_start_at TEXT NOT NULL CHECK (
            {UTC_TEXT_CHECK.format(column='intervention_start_at')}
        ),
        deadline_at TEXT NOT NULL CHECK ({UTC_TEXT_CHECK.format(column='deadline_at')}),
        accepted_actions_json TEXT NOT NULL CHECK (json_valid(accepted_actions_json)),
        parent_scenario TEXT NULL,
        CHECK (json_type(affected_entities_json) = 'array'),
        CHECK (json_type(affected_segments_json) = 'array'),
        CHECK (json_type(affected_features_json) = 'array'),
        CHECK (json_type(accepted_actions_json) = 'array')
    )
    """,
)

OPERATIONAL_TABLES: frozenset[str] = frozenset(
    {
        "episodes",
        "customers",
        "merchants",
        "transactions",
        "transaction_events",
        "label_events",
        "business_calendar",
    }
)
AS_OF_TABLES: frozenset[str] = frozenset(
    {
        "customers",
        "merchants",
        "transactions",
        "transaction_events",
        "label_events",
        "business_calendar",
    }
)
PRIVATE_TABLES: frozenset[str] = frozenset({"private_episodes", "run_metadata", "incident_truth"})

OPERATIONAL_TIMESTAMP_COLUMNS: dict[str, frozenset[str]] = {
    "customers": frozenset({"event_at", "available_at"}),
    "merchants": frozenset({"event_at", "available_at"}),
    "transactions": frozenset(
        {"created_at", "event_at", "available_at"}
    ),
    "transaction_events": frozenset({"event_at", "available_at"}),
    "label_events": frozenset({"event_at", "reported_at", "available_at"}),
    "business_calendar": frozenset({"event_at", "available_at"}),
}
PRIVATE_TIMESTAMP_COLUMNS: dict[str, frozenset[str]] = {
    "incident_truth": frozenset(
        {
            "effect_start_at",
            "effect_end_at",
            "effect_available_at",
            "intervention_start_at",
            "deadline_at",
        }
    )
}


def statements_for(kind: str) -> Iterable[str]:
    """Return DDL for exactly one side of the operational/private boundary."""
    if kind == "operational":
        return OPERATIONAL_DDL
    if kind == "private":
        return PRIVATE_DDL
    raise ValueError("kind must be 'operational' or 'private'")
