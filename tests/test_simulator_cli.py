from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


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

