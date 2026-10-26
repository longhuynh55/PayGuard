from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


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


def _generate(run_dir: Path) -> subprocess.CompletedProcess[str]:
    result = _cli("generate", "--profile", "smoke", "--out", str(run_dir))
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def _fingerprint(run_dir: Path) -> str:
    database = run_dir / "private" / "evaluator.sqlite"
    with sqlite3.connect(database) as connection:
        value = connection.execute(
            "SELECT value_json FROM run_metadata WHERE key='p1.8.run_metadata.v1'"
        ).fetchone()[0]
    return json.loads(value)["canonical_content_fingerprint"]


def test_smoke_cli_generates_separate_stores_reports_and_validates(tmp_path: Path) -> None:
    run_dir = tmp_path / "smoke"
    generated = _generate(run_dir)
    assert "Validation: PASSED" in generated.stdout

    operational_path = run_dir / "operational" / "observations.sqlite"
    private_path = run_dir / "private" / "evaluator.sqlite"
    assert operational_path.is_file()
    assert private_path.is_file()
    assert operational_path.resolve() != private_path.resolve()
    assert (run_dir / "reports" / "smoke.json").is_file()
    assert (run_dir / "private" / "reports" / "smoke_truth.json").is_file()

    public_report = json.loads((run_dir / "reports" / "smoke.json").read_text(encoding="utf-8"))
    calibration = public_report["p2_cohort_support"]["calibration"]
    assert calibration["positive_count"] == 24
    assert calibration["support_status"] == "insufficient"
    assert "calibration_support_25_per_class" in calibration["missing_support"]
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


@pytest.mark.parametrize("violation", ["truth_column", "early_available_at"])
def test_validate_returns_nonzero_for_contract_violations(
    tmp_path: Path, violation: str
) -> None:
    run_dir = tmp_path / violation
    _generate(run_dir)
    database = run_dir / "operational" / "observations.sqlite"
    with sqlite3.connect(database) as connection:
        if violation == "truth_column":
            connection.execute("ALTER TABLE transactions ADD COLUMN fraud_truth INTEGER")
        else:
            connection.execute(
                "UPDATE transactions SET available_at='2000-01-01T00:00:00.000000Z' "
                "WHERE txn_id=(SELECT txn_id FROM transactions ORDER BY txn_id LIMIT 1)"
            )

    validation = _cli("validate", "--run", str(run_dir))
    assert validation.returncode != 0, validation.stdout + validation.stderr
    assert "FAIL" in validation.stdout
    if violation == "truth_column":
        assert "operational_schema" in validation.stdout
    else:
        assert "available_at before event_at" in validation.stdout

