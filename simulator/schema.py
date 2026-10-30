"""SQLite schemas for operational observations and private evaluator data."""

from __future__ import annotations

from collections.abc import Iterable


UTC_TEXT_CHECK = (
    "{column} GLOB "
    "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T"
    "[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'"
)
T0_UTC_TEXT = "2026-01-31T17:00:00.000000Z"

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
        merchant_id TEXT NULL REFERENCES merchants(merchant_id),
        recipient_customer_id TEXT NULL REFERENCES customers(customer_id),
        created_at TEXT NOT NULL CHECK ({UTC_TEXT_CHECK.format(column='created_at')}),
        event_at TEXT NOT NULL CHECK ({UTC_TEXT_CHECK.format(column='event_at')}),
        available_at TEXT NOT NULL CHECK ({UTC_TEXT_CHECK.format(column='available_at')}),
        amount INTEGER NULL CHECK (amount IS NULL OR typeof(amount) = 'integer'),
        channel TEXT NOT NULL CHECK (channel IN ('qr', 'wallet', 'card')),
        UNIQUE (txn_id, episode_id),
        CHECK (event_at = created_at),
        CHECK (
            (merchant_id IS NOT NULL AND recipient_customer_id IS NULL)
            OR (merchant_id IS NULL AND recipient_customer_id IS NOT NULL)
        ),
        CHECK (recipient_customer_id IS NULL OR recipient_customer_id <> customer_id),
        CHECK (recipient_customer_id IS NULL OR channel = 'wallet')
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
        amount INTEGER NULL CHECK (
            amount IS NULL OR (typeof(amount) = 'integer' AND amount > 0)
        ),
        response_code TEXT NULL,
        FOREIGN KEY (txn_id) REFERENCES transactions(txn_id),
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
    f"""
    CREATE TABLE IF NOT EXISTS model_scores (
        txn_id TEXT NOT NULL,
        episode_id TEXT NOT NULL CHECK (episode_id <> 'base'),
        decision_at TEXT NOT NULL CHECK ({UTC_TEXT_CHECK.format(column='decision_at')}),
        event_at TEXT NOT NULL CHECK ({UTC_TEXT_CHECK.format(column='event_at')}),
        available_at TEXT NOT NULL CHECK ({UTC_TEXT_CHECK.format(column='available_at')}),
        raw_score REAL NOT NULL CHECK (raw_score >= 0.0 AND raw_score <= 1.0),
        calibrated_prob REAL NOT NULL CHECK (
            calibrated_prob >= 0.0 AND calibrated_prob <= 1.0
        ),
        conformal_set TEXT NOT NULL CHECK (
            json_valid(conformal_set)
            AND json_type(conformal_set) = 'array'
            AND json(conformal_set) IN ('[]', '["legit"]', '["fraud"]', '["legit","fraud"]')
        ),
        decision_region TEXT NOT NULL CHECK (
            decision_region IN ('legit', 'fraud', 'review')
        ),
        model_version TEXT NOT NULL CHECK (length(model_version) > 0),
        label_policy_version TEXT NOT NULL CHECK (length(label_policy_version) > 0),
        PRIMARY KEY (txn_id, episode_id, model_version),
        FOREIGN KEY (txn_id, episode_id) REFERENCES transactions(txn_id, episode_id),
        CHECK (event_at = decision_at),
        CHECK (available_at >= decision_at),
        CHECK (
            (json(conformal_set) = '["legit"]' AND decision_region = 'legit')
            OR (json(conformal_set) = '["fraud"]' AND decision_region = 'fraud')
            OR (
                json(conformal_set) IN ('[]', '["legit","fraud"]')
                AND decision_region = 'review'
            )
        )
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS daily_metrics (
        metric_id TEXT NOT NULL CHECK (length(metric_id) > 0),
        episode_id TEXT NOT NULL REFERENCES episodes(episode_id) CHECK (episode_id <> 'base'),
        as_of_time TEXT NOT NULL CHECK ({UTC_TEXT_CHECK.format(column='as_of_time')}),
        event_at TEXT NOT NULL CHECK ({UTC_TEXT_CHECK.format(column='event_at')}),
        available_at TEXT NOT NULL CHECK ({UTC_TEXT_CHECK.format(column='available_at')}),
        business_date TEXT NOT NULL CHECK (
            length(business_date) = 10 AND business_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        ),
        feature TEXT NOT NULL CHECK (length(feature) > 0),
        segment TEXT NOT NULL CHECK (length(segment) > 0),
        value REAL NULL CHECK (value IS NULL OR typeof(value) IN ('integer', 'real')),
        reference_window TEXT NOT NULL CHECK (length(reference_window) > 0),
        window_start TEXT NOT NULL CHECK (
            length(window_start) = 10 AND window_start GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        ),
        window_end TEXT NOT NULL CHECK (
            length(window_end) = 10 AND window_end GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        ),
        n_window INTEGER NOT NULL CHECK (typeof(n_window) = 'integer' AND n_window >= 0),
        n_reference INTEGER NOT NULL CHECK (typeof(n_reference) = 'integer' AND n_reference >= 0),
        flags TEXT NOT NULL CHECK (json_valid(flags) AND json_type(flags) = 'array'),
        PRIMARY KEY (metric_id, episode_id, as_of_time, feature, segment),
        CHECK (event_at = as_of_time),
        CHECK (available_at = as_of_time)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS alerts (
        alert_id TEXT NOT NULL PRIMARY KEY CHECK (length(alert_id) > 0),
        episode_id TEXT NOT NULL REFERENCES episodes(episode_id) CHECK (episode_id <> 'base'),
        as_of_time TEXT NOT NULL CHECK ({UTC_TEXT_CHECK.format(column='as_of_time')}),
        metric TEXT NOT NULL CHECK (length(metric) > 0),
        feature TEXT NOT NULL CHECK (length(feature) > 0),
        segment TEXT NOT NULL CHECK (length(segment) > 0),
        value REAL NOT NULL CHECK (typeof(value) IN ('integer', 'real')),
        reference TEXT NOT NULL CHECK (length(reference) > 0),
        threshold REAL NOT NULL CHECK (typeof(threshold) IN ('integer', 'real')),
        event_at TEXT NOT NULL CHECK ({UTC_TEXT_CHECK.format(column='event_at')}),
        available_at TEXT NOT NULL CHECK ({UTC_TEXT_CHECK.format(column='available_at')}),
        UNIQUE (episode_id, as_of_time, metric, feature, segment),
        FOREIGN KEY (metric, episode_id, as_of_time, feature, segment)
            REFERENCES daily_metrics (metric_id, episode_id, as_of_time, feature, segment),
        CHECK (event_at = as_of_time),
        CHECK (available_at = as_of_time)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS incidents (
        incident_id TEXT NOT NULL CHECK (length(incident_id) > 0),
        parent_incident_id TEXT NULL,
        episode_id TEXT NOT NULL REFERENCES episodes(episode_id) CHECK (episode_id <> 'base'),
        alert_ids_json TEXT NOT NULL CHECK (
            json_valid(alert_ids_json) AND json_type(alert_ids_json) = 'array'
        ),
        feature TEXT NOT NULL CHECK (length(feature) > 0),
        segment TEXT NOT NULL CHECK (length(segment) > 0),
        opened_at TEXT NOT NULL CHECK ({UTC_TEXT_CHECK.format(column='opened_at')}),
        status TEXT NOT NULL CHECK (
            status IN ('open', 'recheck', 'close', 'data_ticket', 'fraud_ops', 'human_review')
        ),
        event_at TEXT NOT NULL CHECK ({UTC_TEXT_CHECK.format(column='event_at')}),
        available_at TEXT NOT NULL CHECK ({UTC_TEXT_CHECK.format(column='available_at')}),
        PRIMARY KEY (incident_id, event_at),
        CHECK (parent_incident_id IS NULL OR parent_incident_id <> incident_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS decisions (
        decision_id TEXT NOT NULL PRIMARY KEY CHECK (
            decision_id GLOB 'decision-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'
            || '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'
            || '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'
            || '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'
        ),
        incident_id TEXT NOT NULL CHECK (length(incident_id) > 0),
        episode_id TEXT NOT NULL REFERENCES episodes(episode_id) CHECK (episode_id <> 'base'),
        decision_at TEXT NOT NULL CHECK ({UTC_TEXT_CHECK.format(column='decision_at')}),
        as_of_time TEXT NOT NULL CHECK ({UTC_TEXT_CHECK.format(column='as_of_time')}),
        event_at TEXT NOT NULL CHECK ({UTC_TEXT_CHECK.format(column='event_at')}),
        available_at TEXT NOT NULL CHECK ({UTC_TEXT_CHECK.format(column='available_at')}),
        source TEXT NOT NULL CHECK (source IN ('rule', 'agent')),
        runbook TEXT NULL,
        cause TEXT NOT NULL CHECK (length(cause) > 0),
        evidence_status TEXT NOT NULL CHECK (
            evidence_status IN ('sufficient', 'insufficient', 'conflicting')
        ),
        action TEXT NOT NULL CHECK (
            action IN ('close', 'recheck', 'data_ticket', 'fraud_ops', 'human_review')
        ),
        scope_json TEXT NOT NULL CHECK (
            json_valid(scope_json) AND json_type(scope_json) = 'object'
        ),
        checklist_results_json TEXT NOT NULL CHECK (
            json_valid(checklist_results_json)
            AND json_type(checklist_results_json) = 'object'
        ),
        tool_trace_ids_json TEXT NOT NULL CHECK (
            json_valid(tool_trace_ids_json) AND json_type(tool_trace_ids_json) = 'array'
        ),
        conclusion TEXT NOT NULL CHECK (length(conclusion) > 0),
        reason TEXT NOT NULL CHECK (length(reason) > 0),
        close_score REAL NULL CHECK (
            close_score IS NULL OR (close_score >= 0.0 AND close_score <= 1.0)
        ),
        policy_version TEXT NOT NULL CHECK (length(policy_version) > 0),
        model_slug TEXT NULL CHECK (model_slug IS NULL OR length(model_slug) > 0),
        input_tokens INTEGER NULL CHECK (
            input_tokens IS NULL OR (typeof(input_tokens) = 'integer' AND input_tokens >= 0)
        ),
        output_tokens INTEGER NULL CHECK (
            output_tokens IS NULL OR (typeof(output_tokens) = 'integer' AND output_tokens >= 0)
        ),
        total_tokens INTEGER NULL CHECK (
            total_tokens IS NULL OR (typeof(total_tokens) = 'integer' AND total_tokens >= 0)
        ),
        cost_usd REAL NULL CHECK (
            cost_usd IS NULL OR (
                typeof(cost_usd) IN ('integer', 'real') AND cost_usd >= 0.0
            )
        ),
        CHECK (decision_at = as_of_time),
        CHECK (event_at = decision_at),
        CHECK (available_at = decision_at)
    )
    """,
    "CREATE INDEX IF NOT EXISTS daily_metrics_episode_asof ON daily_metrics (episode_id, event_at, available_at)",
    "CREATE INDEX IF NOT EXISTS alerts_episode_asof ON alerts (episode_id, event_at, available_at)",
    "CREATE INDEX IF NOT EXISTS incidents_episode_asof ON incidents (episode_id, event_at, available_at)",
    "CREATE INDEX IF NOT EXISTS decisions_episode_asof ON decisions (episode_id, event_at, available_at)",
    """
    CREATE INDEX IF NOT EXISTS model_scores_episode_asof
    ON model_scores (episode_id, event_at, available_at)
    """,
    """
    CREATE TRIGGER IF NOT EXISTS model_scores_decision_matches_transaction
    BEFORE INSERT ON model_scores
    WHEN (SELECT created_at FROM transactions
          WHERE txn_id = NEW.txn_id AND episode_id = NEW.episode_id) IS NULL
      OR (SELECT created_at FROM transactions
          WHERE txn_id = NEW.txn_id AND episode_id = NEW.episode_id) <> NEW.decision_at
    BEGIN
        SELECT RAISE(ABORT, 'model score decision_at must match transaction created_at');
    END
    """,
    *(
        f"""
        CREATE TRIGGER IF NOT EXISTS {table}_no_update
        BEFORE UPDATE ON {table}
        BEGIN
            SELECT RAISE(ABORT, '{table} is append-only');
        END
        """
        for table in ("daily_metrics", "alerts", "incidents", "decisions")
    ),
    *(
        f"""
        CREATE TRIGGER IF NOT EXISTS {table}_no_replace
        BEFORE INSERT ON {table}
        WHEN EXISTS (SELECT 1 FROM {table} WHERE {key_predicate})
        BEGIN
            SELECT RAISE(ABORT, '{table} is append-only; keys cannot be replaced');
        END
        """
        for table, key_predicate in (
            (
                "daily_metrics",
                "metric_id = NEW.metric_id AND episode_id = NEW.episode_id "
                "AND as_of_time = NEW.as_of_time AND feature = NEW.feature AND segment = NEW.segment",
            ),
            (
                "alerts",
                "alert_id = NEW.alert_id OR "
                "(episode_id = NEW.episode_id AND as_of_time = NEW.as_of_time "
                "AND metric = NEW.metric AND feature = NEW.feature AND segment = NEW.segment)",
            ),
            ("incidents", "incident_id = NEW.incident_id AND event_at = NEW.event_at"),
            ("decisions", "decision_id = NEW.decision_id"),
        )
    ),
    *(
        f"""
        CREATE TRIGGER IF NOT EXISTS {table}_no_delete
        BEFORE DELETE ON {table}
        BEGIN
            SELECT RAISE(ABORT, '{table} is append-only');
        END
        """
        for table in ("daily_metrics", "alerts", "incidents", "decisions")
    ),
    """
    CREATE TRIGGER IF NOT EXISTS incidents_parent_episode_insert
    BEFORE INSERT ON incidents
    WHEN NEW.parent_incident_id IS NOT NULL
     AND EXISTS (
         SELECT 1 FROM incidents
         WHERE incident_id = NEW.parent_incident_id AND episode_id <> NEW.episode_id
     )
    BEGIN
        SELECT RAISE(ABORT, 'incident parent must belong to the same episode');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS incidents_alert_episode_insert
    BEFORE INSERT ON incidents
    WHEN EXISTS (
        SELECT 1
        FROM json_each(NEW.alert_ids_json) AS incident_alert_id
        LEFT JOIN alerts AS alert
          ON alert.alert_id = incident_alert_id.value
         AND alert.episode_id = NEW.episode_id
        WHERE alert.alert_id IS NULL
    )
    BEGIN
        SELECT RAISE(ABORT, 'incident alerts must exist in the same episode');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS episodes_role_immutable
    BEFORE UPDATE OF episode_id, role ON episodes
    BEGIN
        SELECT RAISE(ABORT, 'episode roles are locked and immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS episodes_no_replace
    BEFORE INSERT ON episodes
    WHEN NEW.episode_id <> 'base'
     AND EXISTS (SELECT 1 FROM episodes WHERE episode_id = NEW.episode_id)
    BEGIN
        SELECT RAISE(ABORT, 'episode roles are locked and immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS episodes_no_delete
    BEFORE DELETE ON episodes
    BEGIN
        SELECT RAISE(ABORT, 'episode roles are locked and immutable');
    END
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS transaction_events_episode_boundary
    BEFORE INSERT ON transaction_events
    WHEN (NEW.episode_id = 'base' AND NEW.event_at >= '{T0_UTC_TEXT}')
      OR (NEW.episode_id <> 'base' AND NEW.event_at < '{T0_UTC_TEXT}')
    BEGIN
        SELECT RAISE(ABORT, 'transaction event episode must match its event_at relative to T0');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS transaction_events_parent_episode
    BEFORE INSERT ON transaction_events
    WHEN EXISTS (SELECT 1 FROM transactions WHERE txn_id = NEW.txn_id)
     AND (
         (NEW.episode_id = 'base'
          AND (SELECT episode_id FROM transactions WHERE txn_id = NEW.txn_id) <> 'base')
         OR
         (NEW.episode_id <> 'base'
          AND (SELECT episode_id FROM transactions WHERE txn_id = NEW.txn_id)
              NOT IN ('base', NEW.episode_id))
     )
    BEGIN
        SELECT RAISE(ABORT, 'transaction event must reference a base or same-episode transaction');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS transaction_events_adjustment_amount
    BEFORE INSERT ON transaction_events
    WHEN (
        NEW.event_type IN ('refunded', 'reversed')
        AND (
            NEW.amount IS NULL
            OR (SELECT amount FROM transactions WHERE txn_id = NEW.txn_id) IS NULL
            OR NEW.amount > (SELECT amount FROM transactions WHERE txn_id = NEW.txn_id)
            OR NEW.event_at <= (SELECT created_at FROM transactions WHERE txn_id = NEW.txn_id)
        )
    ) OR (
        NEW.event_type NOT IN ('refunded', 'reversed')
        AND NEW.amount IS NOT NULL
    )
    BEGIN
        SELECT RAISE(ABORT, 'transaction event amount and time must match refund or reversal rules');
    END
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
    WHEN NEW.merchant_id IS NOT NULL
     AND (SELECT episode_id FROM merchants WHERE merchant_id = NEW.merchant_id)
         NOT IN ('base', NEW.episode_id)
    BEGIN
        SELECT RAISE(ABORT, 'transaction merchant must be base or same episode');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS transactions_recipient_episode_insert
    BEFORE INSERT ON transactions
    WHEN NEW.recipient_customer_id IS NOT NULL
     AND (SELECT episode_id FROM customers WHERE customer_id = NEW.recipient_customer_id)
         NOT IN ('base', NEW.episode_id)
    BEGIN
        SELECT RAISE(ABORT, 'transaction recipient must be base or same episode');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS transactions_identity_immutable
    BEFORE UPDATE OF txn_id, episode_id, customer_id, merchant_id, recipient_customer_id ON transactions
    WHEN NEW.txn_id IS NOT OLD.txn_id
      OR NEW.episode_id IS NOT OLD.episode_id
      OR NEW.customer_id IS NOT OLD.customer_id
      OR NEW.merchant_id IS NOT OLD.merchant_id
      OR NEW.recipient_customer_id IS NOT OLD.recipient_customer_id
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
    """
    CREATE TABLE IF NOT EXISTS episode_manifest (
        episode_id TEXT NOT NULL PRIMARY KEY REFERENCES private_episodes(episode_id),
        role TEXT NOT NULL CHECK (role IN ('development', 'calibration', 'test')),
        seed INTEGER NOT NULL CHECK (typeof(seed) = 'integer' AND seed >= 0),
        template TEXT NOT NULL CHECK (template IN ('clean', 'data_pipeline', 'seasonal')),
        content_fingerprint TEXT NOT NULL CHECK (
            length(content_fingerprint) = 64
            AND content_fingerprint NOT GLOB '*[^0-9a-f]*'
        ),
        config_fingerprint TEXT NOT NULL CHECK (
            length(config_fingerprint) = 64
            AND config_fingerprint NOT GLOB '*[^0-9a-f]*'
        ),
        config_json TEXT NOT NULL CHECK (
            json_valid(config_json) AND json_type(config_json) = 'object'
        )
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS episode_manifest_no_update
    BEFORE UPDATE ON episode_manifest
    BEGIN
        SELECT RAISE(ABORT, 'episode manifest is locked and immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS episode_manifest_no_replace
    BEFORE INSERT ON episode_manifest
    WHEN EXISTS (SELECT 1 FROM episode_manifest WHERE episode_id = NEW.episode_id)
    BEGIN
        SELECT RAISE(ABORT, 'episode manifest is locked and immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS episode_manifest_no_delete
    BEFORE DELETE ON episode_manifest
    BEGIN
        SELECT RAISE(ABORT, 'episode manifest is locked and immutable');
    END
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
        "model_scores",
        "daily_metrics",
        "alerts",
        "incidents",
        "decisions",
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
        "model_scores",
        "daily_metrics",
        "alerts",
        "incidents",
        "decisions",
    }
)
PRIVATE_TABLES: frozenset[str] = frozenset(
    {"private_episodes", "run_metadata", "episode_manifest", "incident_truth"}
)

OPERATIONAL_TIMESTAMP_COLUMNS: dict[str, frozenset[str]] = {
    "customers": frozenset({"event_at", "available_at"}),
    "merchants": frozenset({"event_at", "available_at"}),
    "transactions": frozenset(
        {"created_at", "event_at", "available_at"}
    ),
    "transaction_events": frozenset({"event_at", "available_at"}),
    "label_events": frozenset({"event_at", "reported_at", "available_at"}),
    "business_calendar": frozenset({"event_at", "available_at"}),
    "model_scores": frozenset({"decision_at", "event_at", "available_at"}),
    "daily_metrics": frozenset({"as_of_time", "event_at", "available_at"}),
    "alerts": frozenset({"as_of_time", "event_at", "available_at"}),
    "incidents": frozenset({"opened_at", "event_at", "available_at"}),
    "decisions": frozenset({"decision_at", "as_of_time", "event_at", "available_at"}),
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
