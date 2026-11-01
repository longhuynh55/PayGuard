from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from simulator.validate import validate_run
from simulator.config import load_simulator_config
from simulator.incidents import inject_fraud_surge, inject_new_fraud
from simulator.recipient_features import derive_recipient_signals
from simulator.storage import utc_timestamp


ROOT = Path(__file__).resolve().parents[1]
_KNOWN_CLEAN_MINIMUM_SUPPORT_FP = (
    "ep-0001",
    "incident-c89881b3dc8c2d6e9ebd1354",
)
_EXPECTED_SMOKE_DATA_TICKETS = {
    ("ep-0002", "incident-67c06d4b51c5196eb2894cd0"),
    ("ep-0004", "incident-367c348143656924203a5bc3"),
}
_EXPECTED_SMOKE_AGENT_REVIEW_EPISODES = {"ep-0002", "ep-0003", "ep-0004"}


def _assert_smoke_data_ticket_baseline(
    run_dir: Path, data_ticket_pairs: set[tuple[str, str]]
) -> None:
    assert data_ticket_pairs == _EXPECTED_SMOKE_DATA_TICKETS
    with sqlite3.connect(run_dir / "private" / "evaluator.sqlite") as connection:
        non_pipeline_episode_ids = {
            row[0]
            for row in connection.execute(
                "SELECT episode_id FROM episode_manifest "
                "WHERE template IN ('clean', 'seasonal')"
            )
        }
    assert _KNOWN_CLEAN_MINIMUM_SUPPORT_FP not in data_ticket_pairs
    assert {
        pair for pair in data_ticket_pairs if pair[0] in non_pipeline_episode_ids
    } == set()
    placeholders = ", ".join("?" for _ in non_pipeline_episode_ids)
    with sqlite3.connect(run_dir / "operational" / "observations.sqlite") as connection:
        unsafe_non_pipeline_decisions = connection.execute(
            "SELECT episode_id, incident_id, action FROM decisions "
            f"WHERE episode_id IN ({placeholders}) AND action IN ('data_ticket', 'close')",
            tuple(sorted(non_pipeline_episode_ids)),
        ).fetchall()
    assert unsafe_non_pipeline_decisions == []


def _cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["UV_CACHE_DIR"] = "/tmp/payguard-uv-cache"
    return subprocess.run(
        [sys.executable, "-m", "simulator", *arguments],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _module_cli(module: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["UV_CACHE_DIR"] = "/tmp/payguard-uv-cache"
    return subprocess.run(
        [sys.executable, "-m", module, *arguments],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=240,
    )


def _generate(
    run_dir: Path, *, config: Path | None = None
) -> subprocess.CompletedProcess[str]:
    arguments = ["generate", "--profile", "smoke", "--out", str(run_dir)]
    if config is not None:
        arguments.extend(["--config", str(config)])
    result = _cli(*arguments)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def _fingerprint(run_dir: Path) -> str:
    database = run_dir / "private" / "evaluator.sqlite"
    with sqlite3.connect(database) as connection:
        value = connection.execute(
            "SELECT value_json FROM run_metadata WHERE key='p1.8.run_metadata.v1'"
        ).fetchone()[0]
    return json.loads(value)["canonical_content_fingerprint"]


def test_injected_and_background_card_testing_share_jittered_timing(
    valid_smoke_run: Path, tmp_path: Path,
) -> None:
    simulator_config = load_simulator_config()
    treated_run = tmp_path / "fraud-surge"
    shutil.copytree(valid_smoke_run, treated_run)
    surge_date = simulator_config.timeline.t0_business_date.value + timedelta(days=11)
    with sqlite3.connect(treated_run / "operational" / "observations.sqlite") as operational, sqlite3.connect(
        treated_run / "private" / "evaluator.sqlite"
    ) as private:
        operational.row_factory = sqlite3.Row
        private.row_factory = sqlite3.Row
        surge = inject_fraud_surge(
            operational,
            private,
            simulator_config,
            episode_id="ep-0001",
            incident_truth_id="truth-cli-fraud-surge",
            business_date=surge_date,
            seed=7421,
        )
        operational.commit()
        private.commit()
        card_metadata = json.loads(
            private.execute(
                "SELECT value_json FROM run_metadata "
                "WHERE key='p1.4.card_testing.v1:ep-0001'"
            ).fetchone()[0]
        )
        background_group = next(
            group for group in card_metadata["groups"] if group.get("role") == "fraud"
        )
        background_ids = [
            *background_group["small_transaction_ids"],
            background_group["larger_followup_transaction_id"],
        ]
        target_ids = [*background_ids, *surge.affected_transaction_ids]
        placeholders = ", ".join("?" for _ in target_ids)
        transaction_rows = {
            row["txn_id"]: dict(row)
            for row in operational.execute(
                f"SELECT txn_id, customer_id, merchant_id, created_at, amount "
                f"FROM transactions WHERE txn_id IN ({placeholders})",
                target_ids,
            )
        }
        mechanism = simulator_config.fraud.card_testing
        jitter = simulator_config.fraud.timing_jitter
        base_spacing = mechanism.window_minutes.value * 60 // mechanism.attempts_per_card.value
        background_small = sorted(
            (transaction_rows[txn_id] for txn_id in background_group["small_transaction_ids"]),
            key=lambda row: row["created_at"],
        )
        surge_rows = sorted(
            (transaction_rows[txn_id] for txn_id in surge.affected_transaction_ids),
            key=lambda row: row["created_at"],
        )
        for small_rows, followup_row in (
            (
                background_small,
                transaction_rows[background_group["larger_followup_transaction_id"]],
            ),
            (surge_rows[:-1], surge_rows[-1]),
        ):
            start_local = datetime.fromisoformat(
                small_rows[0]["created_at"].replace("Z", "+00:00")
            ).astimezone(ZoneInfo(simulator_config.timeline.business_timezone.value))
            assert mechanism.start_hour_local.value - jitter.card_testing_start_hour_jitter.value <= start_local.hour
            assert start_local.hour <= mechanism.start_hour_local.value + jitter.card_testing_start_hour_jitter.value + 1
            assert start_local.second > 0
            gaps = [
                int(
                    (
                        datetime.fromisoformat(right["created_at"].replace("Z", "+00:00"))
                        - datetime.fromisoformat(left["created_at"].replace("Z", "+00:00"))
                    ).total_seconds()
                )
                for left, right in zip(small_rows, small_rows[1:])
            ]
            assert all(
                base_spacing - jitter.card_testing_spacing_jitter_seconds.value
                <= gap
                <= base_spacing + jitter.card_testing_spacing_jitter_seconds.value
                for gap in gaps
            )
            start_at = datetime.fromisoformat(
                small_rows[0]["created_at"].replace("Z", "+00:00")
            )
            followup_at = datetime.fromisoformat(
                followup_row["created_at"].replace("Z", "+00:00")
            )
            delay_after_window = int(
                (followup_at - start_at).total_seconds()
                - mechanism.window_minutes.value * 60
            )
            assert (
                mechanism.followup_delay_seconds.value
                - jitter.card_testing_followup_jitter_seconds.value
                <= delay_after_window
                <= mechanism.followup_delay_seconds.value
                + jitter.card_testing_followup_jitter_seconds.value
            )

        bursts = {
            (row[0], row[1], row[2]): (row[3], row[4])
            for row in operational.execute(
                "SELECT customer_id, merchant_id, substr(created_at, 1, 10), "
                "COUNT(*), MIN(created_at) FROM transactions "
                "WHERE episode_id='ep-0001' AND channel='card' "
                "GROUP BY customer_id, merchant_id, substr(created_at, 1, 10) "
                "HAVING COUNT(*) >= ?",
                (mechanism.attempts_per_card.value,),
            )
        }
        for ids in (background_ids, list(surge.affected_transaction_ids)):
            first = min(
                (transaction_rows[txn_id] for txn_id in ids),
                key=lambda row: row["created_at"],
            )
            key = (first["customer_id"], first["merchant_id"], first["created_at"][:10])
            assert key in bursts
            assert bursts[key][0] >= mechanism.attempts_per_card.value
            assert datetime.fromisoformat(bursts[key][1].replace("Z", "+00:00")).second > 0


@pytest.fixture(scope="module")
def valid_smoke_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    run_dir = tmp_path_factory.mktemp("p1_8_smoke") / "smoke"
    _generate(run_dir)
    return run_dir


def test_cli_recipient_signals_separate_cashout_from_business_fanin(
    valid_smoke_run: Path, tmp_path: Path,
) -> None:
    """Injected social engineering and base household activity share fan-in, not outflow."""
    episode_id = "ep-0002"
    simulator_config = load_simulator_config()
    treated_run = tmp_path / "new-fraud-template"
    shutil.copytree(valid_smoke_run, treated_run)
    t0 = utc_timestamp(
        datetime.combine(
            simulator_config.timeline.t0_business_date.value,
            time.min,
            tzinfo=ZoneInfo(simulator_config.timeline.business_timezone.value),
        )
    )
    with sqlite3.connect(treated_run / "operational" / "observations.sqlite") as operational, sqlite3.connect(
        treated_run / "private" / "evaluator.sqlite"
    ) as private:
        operational.row_factory = sqlite3.Row
        private.row_factory = sqlite3.Row
        base_metadata = json.loads(
            private.execute(
                "SELECT value_json FROM run_metadata WHERE key='p1.4.mechanisms.v1'"
            ).fetchone()[0]
        )
        household_group = next(
            group for group in base_metadata["groups"]
            if group["mechanism"] == "household_business_activity"
        )
        business_as_of = utc_timestamp(
            datetime.fromisoformat(t0.replace("Z", "+00:00")) + timedelta(hours=1)
        )
        business_signals = derive_recipient_signals(
            operational, episode_id=episode_id, t0=t0, as_of_time=business_as_of
        )
        business_signal = business_signals[household_group["recipient_customer_id"]]

        inject_new_fraud(
            operational,
            private,
            simulator_config,
            episode_id=episode_id,
            incident_truth_id="truth-cli-new-fraud",
            seed=private.execute(
                "SELECT seed FROM episode_manifest WHERE episode_id = ?",
                (episode_id,),
            ).fetchone()[0],
            transaction_random_keys={},
        )
        private.commit()
        social_metadata = json.loads(
            private.execute(
                "SELECT value_json FROM run_metadata WHERE key='p1.4.mechanisms.v1:ep-0002'"
            ).fetchone()[0]
        )
        social_group = next(
            group for group in social_metadata["groups"]
            if group["mechanism"] == "social_engineering"
        )
        incoming_ids = social_group["incoming_transaction_ids"]
        incoming_placeholders = ", ".join("?" for _ in incoming_ids)
        last_incoming = operational.execute(
            f"SELECT MAX(created_at) FROM transactions WHERE txn_id IN ({incoming_placeholders})",
            incoming_ids,
        ).fetchone()[0]
        social_as_of = utc_timestamp(
            datetime.fromisoformat(last_incoming.replace("Z", "+00:00"))
            + timedelta(hours=23)
        )
        social_signals = derive_recipient_signals(
            operational, episode_id=episode_id, t0=t0, as_of_time=social_as_of
        )
        mule_signal = social_signals[social_group["mule_customer_id"]]
        outgoing_ids = social_group["outgoing_transaction_ids"]
        paired_ids = [*incoming_ids, *outgoing_ids]
        transfer_placeholders = ", ".join("?" for _ in paired_ids)
        paired_times = {
            row["txn_id"]: row["created_at"]
            for row in operational.execute(
                f"SELECT txn_id, created_at FROM transactions WHERE txn_id IN ({transfer_placeholders})",
                paired_ids,
            )
        }
        delays = [
            int(
                (
                    datetime.fromisoformat(paired_times[outgoing].replace("Z", "+00:00"))
                    - datetime.fromisoformat(paired_times[incoming].replace("Z", "+00:00"))
                ).total_seconds()
            )
            for incoming, outgoing in zip(incoming_ids, outgoing_ids, strict=True)
        ]
        assert len(delays) == len(incoming_ids)
        assert simulator_config.fraud.social_engineering.cashout_delay_minutes.value * 60 not in delays
        assert len(set(delays)) > 1

        assert mule_signal.fan_in_7d == business_signal.fan_in_7d == 5
        assert mule_signal.outflow_ratio_24h == 1.0
        assert business_signal.outflow_ratio_24h == 0.0
        assert mule_signal.new_or_dormant is True
        assert business_signal.new_or_dormant is False
        assert mule_signal.older_sender_share_7d == business_signal.older_sender_share_7d
        assert len(mule_signal.first_counterparty_transfer_txn_ids) == 5

        rows = [dict(row) for row in operational.execute(
            "SELECT txn_id, episode_id, customer_id, device_id, card_token, channel, amount "
            "FROM transactions ORDER BY episode_id, event_at, txn_id"
        )]
        assert all(row["device_id"] for row in rows)
        assert all(
            row["card_token"] is None if row["channel"] != "card" else bool(row["card_token"])
            for row in rows
        )
        base_rows = [row for row in rows if row["episode_id"] == "base"]
        normal_base_rows = [
            row for row in base_rows
            if row["channel"] in {"qr", "wallet"}
            and row["amount"] < simulator_config.fraud.near_threshold_minimum_amount.value
        ]
        assert any(
            len({row["device_id"] for row in normal_base_rows if row["customer_id"] == customer_id}) > 1
            for customer_id in {row["customer_id"] for row in normal_base_rows}
        ), "legitimate background accounts should sometimes change devices"

        diagnostic_path = valid_smoke_run / "private" / "reports" / "smoke_truth.json"
        assert diagnostic_path.is_file() and diagnostic_path.stat().st_size > 0
        diagnostics = json.loads(diagnostic_path.read_text(encoding="utf-8"))[
            "p1_4_feature_diagnostics"
        ]
        assert diagnostics
        for diagnostic in diagnostics.values():
            assert diagnostic["id_format_artifacts"]["transaction_format_class_auc"] == 0.5
            assert diagnostic["id_format_artifacts"]["customer_format_class_auc"] == 0.5
            assert diagnostic["auc_by_feature"]["recipient_account_age_days"] is not None
            assert "created_at_second_zero" in diagnostic["auc_by_feature"]
            contrast = diagnostic["by_mechanism"][
                "social_engineering_vs_legit_business_spike"
            ]
            assert "recipient_account_age_days" in contrast["auc_by_feature"]
        base_timing = diagnostics["base"]["fixed_delay_artifacts"]
        for mechanism in ("collusion",):
            artifact = base_timing[mechanism]
            assert artifact["paired_cashout_count"] > 0
            assert artifact["exact_configured_delay_count"] == 0
            assert len(artifact["distinct_delay_seconds"]) > 1
        assert base_timing["social_engineering"]["paired_cashout_count"] == 0
        p14_transaction_ids: set[str] = set()
        with sqlite3.connect(valid_smoke_run / "private" / "evaluator.sqlite") as private:
            for (value_json,) in private.execute(
                "SELECT value_json FROM run_metadata "
                "WHERE key = 'p1.4.mechanisms.v1' OR key LIKE 'p1.4.mechanisms.v1:%'"
            ):
                groups = json.loads(value_json)["groups"]
                for group in groups:
                    for field in (
                        "transaction_ids",
                        "incoming_transaction_ids",
                        "outgoing_transaction_ids",
                    ):
                        p14_transaction_ids.update(group.get(field, []))
        with sqlite3.connect(valid_smoke_run / "operational" / "observations.sqlite") as operational:
            generated_timestamps = {
                txn_id: created_at
                for txn_id, created_at in operational.execute(
                    "SELECT txn_id, created_at FROM transactions "
                    f"WHERE txn_id IN ({', '.join('?' for _ in p14_transaction_ids)})",
                    tuple(sorted(p14_transaction_ids)),
                )
            }
        assert set(generated_timestamps) == p14_transaction_ids
        assert all(
            datetime.fromisoformat(value.replace("Z", "+00:00")).second != 0
            for value in generated_timestamps.values()
        )


@pytest.fixture(scope="module")
def p3_monitoring_smoke_run(
    tmp_path_factory: pytest.TempPathFactory,
    valid_smoke_run: Path,
) -> Path:
    run_dir = tmp_path_factory.mktemp("p3_5_smoke") / "smoke-with-monitoring"
    shutil.copytree(valid_smoke_run, run_dir)
    operational_db = run_dir / "operational" / "observations.sqlite"
    data_root = run_dir.parent / "model-data"
    run_id = "p3-5-validator-smoke"

    trained = _module_cli(
        "model",
        "train",
        "--run-id",
        run_id,
        "--operational-db",
        str(operational_db),
        "--data-root",
        str(data_root),
    )
    assert trained.returncode == 0, trained.stdout + trained.stderr
    scored = _module_cli(
        "model",
        "score",
        "--run-id",
        run_id,
        "--operational-db",
        str(operational_db),
        "--data-root",
        str(data_root),
    )
    assert scored.returncode == 0, scored.stdout + scored.stderr

    output_dir = run_dir.parent / "monitoring-output"
    output_dir.mkdir()
    metrics_csv = output_dir / "daily_metrics.csv"
    metrics = _module_cli(
        "monitoring",
        "metrics",
        "--operational-db",
        str(operational_db),
        "--output",
        str(metrics_csv),
    )
    assert metrics.returncode == 0, metrics.stdout + metrics.stderr
    with metrics_csv.open(encoding="utf-8", newline="") as stream:
        metric_rows = list(csv.DictReader(stream))
    assert metric_rows
    as_of_time = max(row["as_of_time"] for row in metric_rows)

    episode_id = "ep-0002"
    alerts_csv = output_dir / "alerts.csv"
    signaled = _module_cli(
        "monitoring",
        "signals",
        "--operational-db",
        str(operational_db),
        "--episode-id",
        episode_id,
        "--as-of-time",
        as_of_time,
        "--output",
        str(alerts_csv),
        "--report-output",
        str(output_dir / "signals-report.json"),
    )
    assert signaled.returncode == 0, signaled.stdout + signaled.stderr
    with alerts_csv.open(encoding="utf-8", newline="") as stream:
        alert_rows = list(csv.DictReader(stream))
    assert alert_rows

    incidents = _module_cli(
        "monitoring",
        "incidents",
        "--operational-db",
        str(operational_db),
        "--episode-id",
        episode_id,
        "--alerts-csv",
        str(alerts_csv),
        "--output",
        str(output_dir / "incidents.csv"),
    )
    assert incidents.returncode == 0, incidents.stdout + incidents.stderr
    validation = _cli("validate", "--run", str(run_dir))
    assert validation.returncode == 0, validation.stdout + validation.stderr
    assert "PASS monitoring_output_contract" in validation.stdout
    with sqlite3.connect(operational_db) as connection:
        for table in ("daily_metrics", "alerts", "incidents"):
            assert connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] > 0
    return run_dir


def test_smoke_cli_generates_separate_stores_reports_and_validates(valid_smoke_run: Path) -> None:
    run_dir = valid_smoke_run

    operational_path = run_dir / "operational" / "observations.sqlite"
    private_path = run_dir / "private" / "evaluator.sqlite"
    assert operational_path.is_file()
    assert private_path.is_file()
    assert operational_path.resolve() != private_path.resolve()
    assert (run_dir / "reports" / "smoke.json").is_file()
    assert (run_dir / "private" / "reports" / "smoke_truth.json").is_file()

    public_report = json.loads((run_dir / "reports" / "smoke.json").read_text(encoding="utf-8"))
    assert public_report["label_policy_version"] == "mvp-h90-v1"
    assert public_report["model_version"] is None
    assert public_report["label_delay"]["training_snapshot_at_t0"][
        "label_policy_version"
    ] == "mvp-h90-v1"
    assert public_report["label_delay"]["training_snapshot_at_t0"]["model_version"] is None
    calibration = public_report["p2_cohort_support"]["calibration"]
    # F1 removes SE chains from base and non-new_fraud episodes; the old 27 positives
    # become 19, below the locked isotonic calibration minimum of 25.
    assert calibration["positive_count"] == 19
    assert calibration["negative_count"] == 142
    assert calibration["sample_count"] == 161
    assert calibration["support_status"] == "sufficient"
    assert calibration["missing_support"] == []
    assert calibration["calibration_minimum_met"] is False
    assert calibration["warnings"] == [
        "isotonic_support_below_25_per_class; fallback_method=sigmoid"
    ]
    assert "truth_positive_count" not in json.dumps(public_report)
    private_report = json.loads(
        (run_dir / "private" / "reports" / "smoke_truth.json").read_text(encoding="utf-8")
    )
    oracle_calibration_positives = private_report["truth_label_support"][
        "training_and_reference_cohorts"
    ]["calibration"]["truth_positive_count"]
    assert oracle_calibration_positives > calibration["positive_count"]

    validated = _cli("validate", "--run", str(run_dir))
    assert validated.returncode == 0, validated.stdout + validated.stderr
    assert "Validation summary:" in validated.stdout
    assert "report/delay draws reproduce from private stable keys" in validated.stdout
    shape_check = next(
        check
        for check in validate_run(run_dir)["checks"]
        if check["name"] == "operational_identifier_shape_contract"
    )
    assert shape_check["passed"] is True
    affected_ids: set[str] = set()
    with sqlite3.connect(private_path) as private:
        for (value_json,) in private.execute(
            "SELECT value_json FROM run_metadata WHERE key LIKE 'p1.6.incident_effect.v1:%'"
        ):
            affected_ids.update(json.loads(value_json)["affected_transaction_ids"])
    assert affected_ids
    assert all(re.fullmatch(r"txn-[0-9a-f]{64}", txn_id) for txn_id in affected_ids)


@pytest.mark.parametrize(
    ("table", "column", "trigger", "leaked_id"),
    [
        ("transactions", "txn_id", "transactions_identity_immutable", "txn-fraud-only"),
        ("customers", "customer_id", "customers_identity_immutable", "customer-mule"),
    ],
)
def test_validator_rejects_nonuniform_operational_identifier_shapes(
    tmp_path: Path,
    valid_smoke_run: Path,
    table: str,
    column: str,
    trigger: str,
    leaked_id: str,
) -> None:
    run_dir = tmp_path / f"{table}-identifier-leak"
    shutil.copytree(valid_smoke_run, run_dir)
    database = run_dir / "operational" / "observations.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(f"DROP TRIGGER {trigger}")
        original_id = connection.execute(
            f'SELECT "{column}" FROM "{table}" ORDER BY "{column}" LIMIT 1'
        ).fetchone()[0]
        connection.execute(
            f'UPDATE "{table}" SET "{column}" = ? WHERE "{column}" = ?',
            (leaked_id, original_id),
        )

    result = validate_run(run_dir)
    shape_check = next(
        check
        for check in result["checks"]
        if check["name"] == "operational_identifier_shape_contract"
    )
    assert shape_check["passed"] is False


@pytest.mark.parametrize("pipeline_mode", ["null", "multiply_100"])
def test_pipeline_modes_validate_adjustments_against_original_amount(
    tmp_path: Path, pipeline_mode: str
) -> None:
    simulator_source = yaml.safe_load(
        (ROOT / "configs" / "simulator.yaml").read_text(encoding="utf-8")
    )
    simulator_source["transactions"]["refund_probability"]["value"] = 0.2
    simulator_config = tmp_path / "simulator-with-refunds.yaml"
    simulator_config.write_text(
        yaml.safe_dump(simulator_source, sort_keys=False), encoding="utf-8"
    )

    episode_source = yaml.safe_load(
        (ROOT / "configs" / "episodes.yaml").read_text(encoding="utf-8")
    )
    episode_source["templates"]["data_pipeline"]["mode"]["value"] = pipeline_mode
    episode_config = tmp_path / f"episodes-{pipeline_mode}.yaml"
    episode_config.write_text(
        yaml.safe_dump(episode_source, sort_keys=False), encoding="utf-8"
    )

    run_dir = tmp_path / f"pipeline-{pipeline_mode}"
    generated = _cli(
        "generate",
        "--profile",
        "smoke",
        "--out",
        str(run_dir),
        "--config",
        str(simulator_config),
        "--episodes-config",
        str(episode_config),
    )
    assert generated.returncode == 0, generated.stdout + generated.stderr
    assert "Validation summary: 21 passed, 0 failed" in generated.stdout

    with sqlite3.connect(run_dir / "private" / "evaluator.sqlite") as connection:
        effects = [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT value_json FROM run_metadata "
                "WHERE key LIKE 'p1.6.incident_effect.v1:%'"
            )
        ]
    affected_ids = {
        txn_id
        for effect in effects
        for txn_id in effect["affected_transaction_ids"]
    }
    assert affected_ids
    placeholders = ", ".join("?" for _ in affected_ids)
    with sqlite3.connect(run_dir / "operational" / "observations.sqlite") as connection:
        adjusted_affected_count = connection.execute(
            "SELECT COUNT(*) FROM transaction_events "
            "WHERE txn_id IN (" + placeholders + ") "
            "AND event_type IN ('refunded', 'reversed')",
            tuple(sorted(affected_ids)),
        ).fetchone()[0]
        assert adjusted_affected_count > 0
        if pipeline_mode == "null":
            null_adjustment_count = connection.execute(
                "SELECT COUNT(*) FROM transaction_events AS event "
                "JOIN transactions AS tx USING (txn_id) "
                "WHERE tx.amount IS NULL "
                "AND event.event_type IN ('refunded', 'reversed')"
            ).fetchone()[0]
            assert null_adjustment_count > 0

    validated = _cli("validate", "--run", str(run_dir))
    assert validated.returncode == 0, validated.stdout + validated.stderr
    assert "Validation summary: 21 passed, 0 failed" in validated.stdout


def test_generate_checks_episode_duration_against_selected_simulator_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import simulator.__main__ as simulator_cli
    from simulator.config import load_episode_config, load_simulator_config

    selected_config = tmp_path / "selected-simulator.yaml"
    shutil.copyfile(ROOT / "configs" / "simulator.yaml", selected_config)
    base_config = load_simulator_config(ROOT / "configs" / "simulator.yaml")
    episode_config = load_episode_config(ROOT / "configs" / "episodes.yaml")
    mismatched_runtime = base_config.episode.duration_days.model_copy(
        update={"value": episode_config.protocol.duration_days.value - 1}
    )
    mismatched_simulator = base_config.model_copy(
        update={
            "episode": base_config.episode.model_copy(
                update={"duration_days": mismatched_runtime}
            )
        }
    )
    loaded_paths: list[Path] = []

    def load_selected_config(path: str | Path):
        loaded_paths.append(Path(path))
        return mismatched_simulator

    monkeypatch.setattr(simulator_cli, "load_simulator_config", load_selected_config)
    result = simulator_cli.main(
        [
            "generate",
            "--profile",
            "smoke",
            "--out",
            str(tmp_path / "run"),
            "--config",
            str(selected_config),
            "--episodes-config",
            str(ROOT / "configs" / "episodes.yaml"),
        ]
    )

    assert result == 2
    assert loaded_paths == [selected_config]
    assert "must match episodes protocol.duration_days" in capsys.readouterr().err


def test_validate_accepts_populated_monitoring_tables(p3_monitoring_smoke_run: Path) -> None:
    validated = _cli("validate", "--run", str(p3_monitoring_smoke_run))
    assert validated.returncode == 0, validated.stdout + validated.stderr
    assert "PASS monitoring_output_contract" in validated.stdout
    assert "FAIL" not in validated.stdout


def test_triage_cli_smoke_routes_pipeline_and_is_repeatable(
    tmp_path: Path, valid_smoke_run: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PAYGUARD_BENCHMARK_STATE_DIR", str(tmp_path / "benchmark-state"))
    run_dir = tmp_path / "p4-smoke"
    shutil.copytree(valid_smoke_run, run_dir)
    database = run_dir / "operational" / "observations.sqlite"

    from monitoring.metrics import compute_daily_metrics
    from monitoring.signals import compute_alerts
    from monitoring.store import persist_monitoring_rows
    from simulator.config import load_simulator_config

    simulator_config = load_simulator_config()
    import simulator.config as simulator_config_module
    import monitoring.metrics as monitoring_metrics_module

    original_read_yaml = simulator_config_module._read_yaml

    def guard_episode_yaml(path: str | Path):
        if "episodes.yaml" in str(path):
            raise AssertionError("monitoring and triage must not read configs/episodes.yaml")
        return original_read_yaml(path)

    monkeypatch.setattr(simulator_config_module, "_read_yaml", guard_episode_yaml)

    def forbidden_episode_loader(*args, **kwargs):
        raise AssertionError("monitoring and triage must not load configs/episodes.yaml")

    monkeypatch.setattr(simulator_config_module, "load_episode_config", forbidden_episode_loader)
    monkeypatch.setattr(
        monitoring_metrics_module,
        "load_episode_config",
        forbidden_episode_loader,
        raising=False,
    )
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        episode_ids = tuple(
            row[0]
            for row in connection.execute(
                "SELECT episode_id FROM episodes WHERE episode_id <> 'base' ORDER BY episode_id"
            )
        )
        metrics = compute_daily_metrics(
            connection,
            simulator_config=simulator_config,
            episode_ids=episode_ids,
        )
        persist_monitoring_rows(connection, "daily_metrics", metrics)
        cutoff = max(row["as_of_time"] for row in metrics)
        for episode_id in episode_ids:
            episode_metrics = [
                row for row in metrics if row["episode_id"] == episode_id
            ]
            alerts = compute_alerts(
                episode_metrics,
                episode_id=episode_id,
                as_of_time=cutoff,
            )
            persist_monitoring_rows(connection, "alerts", alerts.alerts)
            from monitoring.incidents import compute_incidents

            compute_incidents(alerts.alerts, episode_id=episode_id)

    from triage.runner import run_triage
    from triage.__main__ import main as triage_main

    first_result = run_triage(run_dir)
    assert first_result["decision_rows_total"] > 0
    assert triage_main(["run", "--run", str(run_dir)]) == 0

    with sqlite3.connect(database) as connection:
        before = connection.execute(
            "SELECT * FROM decisions ORDER BY decision_id"
        ).fetchall()

    first = _module_cli("triage", "run", "--run", str(run_dir))
    assert first.returncode == 0, first.stdout + first.stderr
    assert "P4 triage completed" in first.stdout

    with sqlite3.connect(database) as connection:
        data_ticket_pairs = set(
            connection.execute(
                "SELECT DISTINCT episode_id, incident_id FROM decisions "
                "WHERE action='data_ticket'"
            ).fetchall()
        )
        agent_reviews = connection.execute(
            "SELECT reason, evidence_status FROM decisions "
            "WHERE action='human_review' AND source='agent'"
        ).fetchall()
        decision_columns = {
            row[1] for row in connection.execute('PRAGMA table_info("decisions")')
        }
    _assert_smoke_data_ticket_baseline(run_dir, data_ticket_pairs)
    assert agent_reviews
    assert all("agent_not_configured" in reason for reason, _ in agent_reviews)
    assert all(status in {"sufficient", "insufficient", "conflicting"} for _, status in agent_reviews)
    assert not decision_columns.intersection(
        {"truth", "incident_truth", "mechanism", "template", "seed", "accepted_actions_json"}
    )

    with sqlite3.connect(database) as connection:
        after = connection.execute(
            "SELECT * FROM decisions ORDER BY decision_id"
        ).fetchall()
    assert after == before

    validated = _cli("validate", "--run", str(run_dir))
    assert validated.returncode == 0, validated.stdout + validated.stderr
    assert "PASS decision_output_contract" in validated.stdout


def test_fake_agent_smoke_routes_remaining_incidents_and_evaluates(
    tmp_path: Path,
    valid_smoke_run: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PAYGUARD_BENCHMARK_STATE_DIR", str(tmp_path / "benchmark-state"))
    run_dir = tmp_path / "p4-fake-agent-smoke"
    shutil.copytree(valid_smoke_run, run_dir)
    database = run_dir / "operational" / "observations.sqlite"
    import payguard.identity as identity
    from payguard.identity import create_lock

    monkeypatch.setattr(identity, "_dirty_entries", lambda: ())
    create_lock(run_dir, "prompt-rule", profile="smoke")
    create_lock(
        run_dir,
        "policy",
        policy_name="rule_agent_fake",
        profile="smoke",
    )

    from monitoring.metrics import compute_daily_metrics
    from monitoring.signals import compute_alerts
    from monitoring.store import persist_monitoring_rows
    from simulator.config import load_simulator_config

    simulator_config = load_simulator_config()
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        episode_ids = tuple(
            row[0]
            for row in connection.execute(
                "SELECT episode_id FROM episodes WHERE episode_id <> 'base' ORDER BY episode_id"
            )
        )
        metrics = compute_daily_metrics(
            connection,
            simulator_config=simulator_config,
            episode_ids=episode_ids,
        )
        persist_monitoring_rows(connection, "daily_metrics", metrics)
        cutoff = max(row["as_of_time"] for row in metrics)
        for episode_id in episode_ids:
            episode_metrics = [
                row for row in metrics if row["episode_id"] == episode_id
            ]
            alerts = compute_alerts(
                episode_metrics,
                episode_id=episode_id,
                as_of_time=cutoff,
            )
            persist_monitoring_rows(connection, "alerts", alerts.alerts)

    switched_provider_run_dir = tmp_path / "p4-provider-switch"
    shutil.copytree(run_dir, switched_provider_run_dir)
    from triage.runner import run_triage

    unconfigured = run_triage(switched_provider_run_dir)
    assert unconfigured["decision_rows_inserted"] > 0
    switched = _module_cli(
        "triage",
        "run",
        "--run",
        str(switched_provider_run_dir),
        "--agent-provider",
        "fake",
    )
    assert switched.returncode != 0
    assert "fresh copy of the run directory per provider" in (
        switched.stdout + switched.stderr
    )

    first = _module_cli(
        "triage",
        "run",
        "--run",
        str(run_dir),
        "--agent-provider",
        "fake",
        "--episode-role",
        "development",
        "--episode-role",
        "calibration",
        "--episode-role",
        "test",
    )
    assert first.returncode == 0, first.stdout + first.stderr
    with sqlite3.connect(database) as connection:
        before = connection.execute(
            "SELECT * FROM decisions ORDER BY decision_id"
        ).fetchall()
        rule_data_ticket_pairs = set(
            connection.execute(
                "SELECT DISTINCT episode_id, incident_id FROM decisions "
                "WHERE source='rule' AND action='data_ticket'"
            ).fetchall()
        )
        all_data_ticket_pairs = set(
            connection.execute(
                "SELECT DISTINCT episode_id, incident_id FROM decisions "
                "WHERE action='data_ticket'"
            ).fetchall()
        )
        rule_slugs = connection.execute(
            "SELECT model_slug FROM decisions WHERE source='rule'"
        ).fetchall()
        duplicate_rule_groups = connection.execute(
            "SELECT COUNT(*) FROM (SELECT incident_id, episode_id, as_of_time "
            "FROM decisions WHERE source='rule' GROUP BY incident_id, episode_id, as_of_time "
            "HAVING COUNT(*) > 1)"
        ).fetchone()[0]
        agent_rows = connection.execute(
            "SELECT episode_id, tool_trace_ids_json, input_tokens, output_tokens, total_tokens, "
            "cost_usd, model_slug, action FROM decisions WHERE source='agent' "
            "AND model_slug='fake/payguard-p4-round1' "
            "ORDER BY episode_id"
        ).fetchall()

    _assert_smoke_data_ticket_baseline(run_dir, all_data_ticket_pairs)
    assert rule_data_ticket_pairs == _EXPECTED_SMOKE_DATA_TICKETS
    assert len(rule_slugs) == len(rule_data_ticket_pairs) and all(
        row[0] is None for row in rule_slugs
    )
    assert duplicate_rule_groups == 0
    assert {row[0] for row in agent_rows} == _EXPECTED_SMOKE_AGENT_REVIEW_EPISODES
    for _, trace_ids, input_tokens, output_tokens, total_tokens, cost_usd, model_slug, action in agent_rows:
        assert len(json.loads(trace_ids)) == 1
        assert input_tokens > 0 and output_tokens > 0 and total_tokens == input_tokens + output_tokens
        assert cost_usd > 0
        assert model_slug == "fake/payguard-p4-round1"
        assert action == "human_review"

    replay = _module_cli(
        "triage",
        "run",
        "--run",
        str(run_dir),
        "--agent-provider",
        "fake",
        "--episode-role",
        "development",
        "--episode-role",
        "calibration",
        "--episode-role",
        "test",
    )
    assert replay.returncode == 0, replay.stdout + replay.stderr
    with sqlite3.connect(database) as connection:
        after = connection.execute(
            "SELECT * FROM decisions ORDER BY decision_id"
        ).fetchall()
    assert after == before

    evaluated = _module_cli(
        "evaluation",
        "decisions",
        "--run",
        str(run_dir),
        "--episode-role",
        "development",
        "--episode-role",
        "calibration",
        "--episode-role",
        "test",
    )
    assert evaluated.returncode == 0, evaluated.stdout + evaluated.stderr
    report_path = run_dir / "private" / "reports" / "p4_5_decisions.json"
    assert report_path.is_file()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["decision_counts"]["total"] == len(before)

    validated = _cli("validate", "--run", str(run_dir))
    assert validated.returncode == 0, validated.stdout + validated.stderr
    assert "PASS decision_output_contract" in validated.stdout

    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER decisions_no_update")
        rule_decision = connection.execute(
            "SELECT decision_id FROM decisions WHERE source='rule' LIMIT 1"
        ).fetchone()
        assert rule_decision is not None, "smoke baseline must include a rule-issued decision"
        rule_decision_id = rule_decision[0]
        connection.execute(
            "UPDATE decisions SET model_slug='fake/payguard-p4-round1' WHERE decision_id=?",
            (rule_decision_id,),
        )
    invalidated = _cli("validate", "--run", str(run_dir))
    assert invalidated.returncode != 0
    assert "rule decision" in invalidated.stdout + invalidated.stderr
    assert "must have NULL model_slug" in invalidated.stdout + invalidated.stderr


@pytest.mark.parametrize("violation", ["available_at_differs", "cross_episode_alert"])
def test_validate_rejects_corrupt_monitoring_alerts(
    tmp_path: Path,
    p3_monitoring_smoke_run: Path,
    violation: str,
) -> None:
    run_dir = tmp_path / violation
    shutil.copytree(p3_monitoring_smoke_run, run_dir)
    database = run_dir / "operational" / "observations.sqlite"
    with sqlite3.connect(database) as connection:
        alert = connection.execute(
            "SELECT alert_id, episode_id, as_of_time, metric, feature, segment "
            "FROM alerts ORDER BY alert_id LIMIT 1"
        ).fetchone()
        assert alert is not None
        connection.execute("DROP TRIGGER alerts_no_update")
        if violation == "available_at_differs":
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute(
                "UPDATE alerts SET available_at='2026-03-03T00:00:00.000000Z' "
                "WHERE alert_id=?",
                (alert[0],),
            )
        else:
            other_episode = connection.execute(
                "SELECT e.episode_id FROM episodes AS e "
                "WHERE e.episode_id <> ? AND e.episode_id <> 'base' "
                "AND NOT EXISTS (SELECT 1 FROM alerts AS other "
                "WHERE other.episode_id=e.episode_id AND other.as_of_time=? "
                "AND other.metric=? AND other.feature=? AND other.segment=?) "
                "ORDER BY e.episode_id LIMIT 1",
                (alert[1], alert[2], alert[3], alert[4], alert[5]),
            ).fetchone()
            assert other_episode is not None
            connection.execute(
                "UPDATE alerts SET episode_id=? WHERE alert_id=?",
                (other_episode[0], alert[0]),
            )

    validated = _cli("validate", "--run", str(run_dir))
    assert validated.returncode != 0, validated.stdout + validated.stderr
    assert "FAIL monitoring_output_contract" in validated.stdout
    if violation == "available_at_differs":
        assert "as_of_time" in validated.stdout
    else:
        assert "another episode" in validated.stdout


def test_validate_rejects_incident_snapshot_with_future_alert(
    tmp_path: Path,
    p3_monitoring_smoke_run: Path,
) -> None:
    run_dir = tmp_path / "future-alert-in-incident"
    shutil.copytree(p3_monitoring_smoke_run, run_dir)
    database = run_dir / "operational" / "observations.sqlite"
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        incident = dict(
            connection.execute(
                "SELECT * FROM incidents ORDER BY incident_id, event_at LIMIT 1"
            ).fetchone()
        )
        incident_alert_ids = json.loads(incident["alert_ids_json"])
        first_alert = dict(
            connection.execute(
                "SELECT * FROM alerts WHERE alert_id=?", (incident_alert_ids[0],)
            ).fetchone()
        )
        source_metric = dict(
            connection.execute(
                "SELECT * FROM daily_metrics WHERE metric_id=? AND episode_id=? "
                "AND as_of_time=? AND feature=? AND segment=?",
                (
                    first_alert["metric"],
                    first_alert["episode_id"],
                    first_alert["as_of_time"],
                    first_alert["feature"],
                    first_alert["segment"],
                ),
            ).fetchone()
        )
        future_as_of = (
            datetime.fromisoformat(source_metric["as_of_time"].replace("Z", "+00:00"))
            + timedelta(seconds=1)
        ).isoformat(timespec="microseconds").replace("+00:00", "Z")

        future_metric = dict(source_metric)
        future_metric.update(
            as_of_time=future_as_of,
            event_at=future_as_of,
            available_at=future_as_of,
        )
        metric_columns = tuple(future_metric)
        connection.execute(
            f'INSERT INTO daily_metrics ({", ".join(metric_columns)}) '
            f'VALUES ({", ".join("?" for _ in metric_columns)})',
            tuple(future_metric[column] for column in metric_columns),
        )

        identity = {
            "schema_version": 1,
            "episode_id": first_alert["episode_id"],
            "as_of_time": future_as_of,
            "metric": first_alert["metric"],
            "feature": first_alert["feature"],
            "segment": first_alert["segment"],
        }
        canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        future_alert = dict(first_alert)
        future_alert.update(
            alert_id="alert-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24],
            as_of_time=future_as_of,
            event_at=future_as_of,
            available_at=future_as_of,
        )
        alert_columns = tuple(future_alert)
        connection.execute(
            f'INSERT INTO alerts ({", ".join(alert_columns)}) '
            f'VALUES ({", ".join("?" for _ in alert_columns)})',
            tuple(future_alert[column] for column in alert_columns),
        )

        connection.execute("DROP TRIGGER incidents_no_update")
        connection.execute(
            "UPDATE incidents SET alert_ids_json=? WHERE incident_id=? AND event_at=?",
            (
                json.dumps(
                    [*incident_alert_ids, future_alert["alert_id"]],
                    separators=(",", ":"),
                ),
                incident["incident_id"],
                incident["event_at"],
            ),
        )

    validated = _cli("validate", "--run", str(run_dir))
    assert validated.returncode != 0, validated.stdout + validated.stderr
    assert "FAIL monitoring_output_contract" in validated.stdout
    assert "snapshot is available before alert" in validated.stdout


def test_validate_rejects_incident_snapshot_before_opened_at(
    tmp_path: Path,
    p3_monitoring_smoke_run: Path,
) -> None:
    run_dir = tmp_path / "incident-before-opened-at"
    shutil.copytree(p3_monitoring_smoke_run, run_dir)
    database = run_dir / "operational" / "observations.sqlite"
    with sqlite3.connect(database) as connection:
        incident = connection.execute(
            "SELECT incident_id, event_at, opened_at FROM incidents "
            "ORDER BY incident_id, event_at LIMIT 1"
        ).fetchone()
        assert incident is not None
        before_opened = (
            datetime.fromisoformat(incident[2].replace("Z", "+00:00"))
            - timedelta(seconds=1)
        ).isoformat(timespec="microseconds").replace("+00:00", "Z")
        connection.execute("DROP TRIGGER incidents_no_update")
        connection.execute(
            "UPDATE incidents SET event_at=?, available_at=? "
            "WHERE incident_id=? AND event_at=?",
            (before_opened, before_opened, incident[0], incident[1]),
        )

    validated = _cli("validate", "--run", str(run_dir))
    assert validated.returncode != 0, validated.stdout + validated.stderr
    assert "FAIL monitoring_output_contract" in validated.stdout
    assert "opened_at <= event_at <= available_at" in validated.stdout


@pytest.mark.parametrize(
    "field,payload",
    [
        (
            "flags",
            json.dumps(
                [
                    "accepted_actions=data_ticket",
                    "requires_intervention=1",
                    "effect_available_at=2026-02-07T00:00:00.000000Z",
                ],
                separators=(",", ":"),
            ),
        ),
        ("feature", "accepted_actions"),
    ],
)
def test_validate_rejects_private_fields_in_monitoring_values(
    tmp_path: Path,
    p3_monitoring_smoke_run: Path,
    field: str,
    payload: str,
) -> None:
    run_dir = tmp_path / f"private-field-in-{field}"
    shutil.copytree(p3_monitoring_smoke_run, run_dir)
    database = run_dir / "operational" / "observations.sqlite"
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        metric = dict(
            connection.execute(
                "SELECT * FROM daily_metrics ORDER BY metric_id, as_of_time LIMIT 1"
            ).fetchone()
        )
        metric["segment"] = "segment-private-marker-test"
        metric[field] = payload if field == "flags" else payload
        metric_columns = tuple(metric)
        connection.execute(
            f'INSERT INTO daily_metrics ({", ".join(metric_columns)}) '
            f'VALUES ({", ".join("?" for _ in metric_columns)})',
            tuple(metric[column] for column in metric_columns),
        )

    validated = _cli("validate", "--run", str(run_dir))
    assert validated.returncode != 0, validated.stdout + validated.stderr
    assert "FAIL monitoring_output_contract" in validated.stdout
    if field == "flags":
        assert "unsupported quality flags" in validated.stdout
    else:
        assert "private truth or injector field name" in validated.stdout


def test_same_smoke_config_has_same_canonical_fingerprint(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _generate(first)
    _generate(second)

    assert _fingerprint(first) == _fingerprint(second)


@pytest.mark.parametrize(
    "violation",
    [
        "truth_column",
        "early_available_at",
        "missing_incident_truth",
        "wrong_accepted_actions",
        "wrong_incident_deadline",
    ],
)
def test_validate_returns_nonzero_for_contract_violations(
    tmp_path: Path, valid_smoke_run: Path, violation: str
) -> None:
    run_dir = tmp_path / violation
    shutil.copytree(valid_smoke_run, run_dir)
    database = run_dir / "operational" / "observations.sqlite"
    private_database = run_dir / "private" / "evaluator.sqlite"
    if violation in {"truth_column", "early_available_at"}:
        with sqlite3.connect(database) as connection:
            if violation == "truth_column":
                connection.execute("ALTER TABLE transactions ADD COLUMN fraud_truth INTEGER")
            else:
                connection.execute(
                    "UPDATE transactions SET available_at='2000-01-01T00:00:00.000000Z' "
                    "WHERE txn_id=(SELECT txn_id FROM transactions ORDER BY txn_id LIMIT 1)"
                )
    else:
        with sqlite3.connect(private_database) as connection:
            if violation == "missing_incident_truth":
                connection.execute(
                    "DELETE FROM incident_truth WHERE incident_truth_id = "
                    "(SELECT incident_truth_id FROM incident_truth WHERE cause='data_pipeline' LIMIT 1)"
                )
            elif violation == "wrong_accepted_actions":
                connection.execute(
                    "UPDATE incident_truth SET accepted_actions_json='[\"close\"]' "
                    "WHERE cause='data_pipeline'"
                )
            elif violation == "wrong_incident_deadline":
                connection.execute(
                    "UPDATE incident_truth SET deadline_at='2000-01-01T00:00:00.000000Z' "
                    "WHERE cause='data_pipeline'"
                )

    validation = _cli("validate", "--run", str(run_dir))
    assert validation.returncode != 0, validation.stdout + validation.stderr
    assert "FAIL" in validation.stdout
    if violation == "truth_column":
        assert "operational_schema" in validation.stdout
    elif violation == "early_available_at":
        assert "available_at before event_at" in validation.stdout
    else:
        assert "incident_truth_contract" in validation.stdout


def test_validate_rejects_incident_truth_shifted_outside_operational_effects(
    tmp_path: Path, valid_smoke_run: Path
) -> None:
    run_dir = tmp_path / "shifted_incident_truth"
    shutil.copytree(valid_smoke_run, run_dir)
    private_database = run_dir / "private" / "evaluator.sqlite"
    with sqlite3.connect(private_database) as connection:
        row = connection.execute(
            "SELECT incident_truth_id, effect_start_at, effect_end_at, effect_available_at, "
            "intervention_start_at, deadline_at FROM incident_truth WHERE cause='data_pipeline'"
        ).fetchone()
        assert row is not None
        fields = (
            "effect_start_at",
            "effect_end_at",
            "effect_available_at",
            "intervention_start_at",
            "deadline_at",
        )
        shifted_values = [
            (datetime.fromisoformat(value.replace("Z", "+00:00")) + timedelta(days=365))
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
            for value in row[1:]
        ]
        connection.execute(
            "UPDATE incident_truth SET "
            + ", ".join(f"{field}=?" for field in fields)
            + " WHERE incident_truth_id=?",
            (*shifted_values, row[0]),
        )

    result = validate_run(run_dir)
    assert result["status"] == "failed"
    incident_check = next(
        check for check in result["checks"] if check["name"] == "incident_truth_contract"
    )
    assert incident_check["passed"] is False
    assert "does not match affected operational transactions" in incident_check["detail"]

    validation = _cli("validate", "--run", str(run_dir))
    assert validation.returncode != 0, validation.stdout + validation.stderr
    assert "FAIL incident_truth_contract" in validation.stdout


def test_validate_rejects_transaction_events_outside_episode_window(
    tmp_path: Path, valid_smoke_run: Path
) -> None:
    run_dir = tmp_path / "late_transaction_event"
    shutil.copytree(valid_smoke_run, run_dir)
    database = run_dir / "operational" / "observations.sqlite"
    config = load_simulator_config()
    episode_start = datetime.combine(
        config.timeline.t0_business_date.value,
        time.min,
        tzinfo=ZoneInfo(config.timeline.business_timezone.value),
    ).astimezone(ZoneInfo("UTC"))
    episode_end = (
        episode_start + timedelta(days=config.episode.duration_days.value)
    ).isoformat(timespec="microseconds").replace("+00:00", "Z")
    with sqlite3.connect(database) as connection:
        txn_id = connection.execute(
            "SELECT txn_id FROM transactions WHERE episode_id = 'ep-0001' ORDER BY txn_id LIMIT 1"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO transaction_events "
            "(event_id, txn_id, episode_id, event_type, event_at, available_at, amount, response_code) "
            "VALUES (?, ?, 'ep-0001', 'authorized', ?, ?, NULL, NULL)",
            (f"{txn_id}:late-authorized", txn_id, episode_end, episode_end),
        )

    result = validate_run(run_dir)
    assert result["status"] == "failed"
    incident_check = next(
        check
        for check in result["checks"]
        if check["name"] == "transaction_event_episode_window"
    )
    assert incident_check["passed"] is False
    assert "event_at is outside the episode future window" in incident_check["detail"]


def test_zero_report_probability_blocks_p2_handoff(tmp_path: Path) -> None:
    source = yaml.safe_load((ROOT / "configs" / "simulator.yaml").read_text(encoding="utf-8"))
    source["label_reporting"]["probability_reported"]["value"] = 0.0
    config_path = tmp_path / "no-reported-labels.yaml"
    config_path.write_text(yaml.safe_dump(source, sort_keys=False), encoding="utf-8")
    run_dir = tmp_path / "no-reported-labels"

    generated = _cli(
        "generate",
        "--profile",
        "smoke",
        "--out",
        str(run_dir),
        "--config",
        str(config_path),
    )
    assert generated.returncode != 0, generated.stdout + generated.stderr
    assert "FAIL p2_handoff_support" in generated.stdout
    public_report = json.loads((run_dir / "reports" / "smoke.json").read_text(encoding="utf-8"))
    assert public_report["p2_cohort_support"]["fit"]["positive_count"] == 0
    assert public_report["p2_cohort_support"]["validation"]["positive_count"] == 0
    assert public_report["p2_cohort_support"]["calibration"]["positive_count"] == 0
    assert public_report["p2_cohort_support"]["reference_drift"]["transaction_count"] > 0

    validation = _cli("validate", "--run", str(run_dir))
    assert validation.returncode != 0, validation.stdout + validation.stderr
    assert "FAIL p2_handoff_support" in validation.stdout

