from __future__ import annotations

import csv
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

from simulator.validate import validate_run


ROOT = Path(__file__).resolve().parents[1]


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


@pytest.fixture(scope="module")
def valid_smoke_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    run_dir = tmp_path_factory.mktemp("p1_8_smoke") / "smoke"
    _generate(run_dir)
    return run_dir


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
    assert calibration["positive_count"] == 24
    assert calibration["support_status"] == "sufficient"
    assert calibration["missing_support"] == []
    assert calibration["calibration_minimum_met"] is False
    assert any(
        "isotonic_support_below_25_per_class" in warning
        and "fallback_method=sigmoid" in warning
        for warning in calibration["warnings"]
    )
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


def test_validate_accepts_populated_monitoring_tables(p3_monitoring_smoke_run: Path) -> None:
    validated = _cli("validate", "--run", str(p3_monitoring_smoke_run))
    assert validated.returncode == 0, validated.stdout + validated.stderr
    assert "PASS monitoring_output_contract" in validated.stdout
    assert "FAIL" not in validated.stdout


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

