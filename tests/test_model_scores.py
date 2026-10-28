from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3
import subprocess
import sys

import joblib
import numpy as np
import pytest

from model.config import ConformalConfig
from model.conformal import decision_for_set, fit_mondrian_conformal, predict_sets
from model.__main__ import _parser as model_parser
from model.__main__ import main as model_main
from model.evaluate import _metric_block
from model.features import build_score_feature_matrix
import simulator.config as simulator_config_module
from simulator.asof import read_as_of
from simulator.config import load_episode_config, load_simulator_config
from simulator.episodes import generate_episodes
from simulator.storage import create_export_stores, insert_operational_record


ROOT = Path(__file__).resolve().parents[1]
T0 = "2026-01-31T17:00:00.000000Z"


@pytest.fixture
def insufficient_support_conformal():
    return fit_mondrian_conformal(
        (0.1, 0.9),
        (0, 1),
        ConformalConfig(
            alpha_legit=0.005,
            alpha_fraud=0.20,
            provenance="round-one test fixture",
        ),
    )


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "model", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )


def test_smoke_cli_scores_episode_asof_and_evaluates_private_truth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = create_export_stores(tmp_path / "smoke-run")
    simulator_config = load_simulator_config(ROOT / "configs" / "simulator.yaml")
    episode_config = load_episode_config(ROOT / "configs" / "episodes.yaml")
    try:
        generated = generate_episodes(
            store.operational,
            store.private,
            simulator_config,
            episode_config,
        )
        expected_counts = {
            row["episode_id"]: row["transaction_count"]
            for row in store.operational.execute(
                "SELECT episode_id, COUNT(*) AS transaction_count FROM transactions "
                "WHERE episode_id <> 'base' GROUP BY episode_id"
            )
        }
        assert set(expected_counts) == {episode.episode_id for episode in generated.episodes}
        store.commit()
        operational_path = store.operational_path
        private_path = store.private_path
    finally:
        store.close()

    data_root = tmp_path / "data"
    run_id = "p2-5-smoke"
    _cli(
        "train",
        "--run-id", run_id,
        "--operational-db", str(operational_path),
        "--data-root", str(data_root),
    )
    scored = _cli(
        "score",
        "--run-id", run_id,
        "--operational-db", str(operational_path),
        "--data-root", str(data_root),
    )
    score_summary = json.loads(scored.stdout)
    assert score_summary["episode_counts"] == expected_counts
    assert score_summary["score_count"] == sum(expected_counts.values())

    model_directory = data_root / run_id / "model"
    score_csv = model_directory / "model_scores.csv"
    first_csv = score_csv.read_bytes()
    with pytest.raises(SystemExit):
        model_parser().parse_args(
            [
                "score",
                "--run-id",
                run_id,
                "--operational-db",
                str(operational_path),
                "--episode-config",
                "configs/episodes.yaml",
            ]
        )

    original_read_yaml = simulator_config_module._read_yaml

    def forbid_injector_config(path: str | Path):
        if "episodes.yaml" in str(path):
            raise AssertionError(f"model score/evaluate attempted to read private config: {path}")
        return original_read_yaml(path)

    monkeypatch.setattr(simulator_config_module, "_read_yaml", forbid_injector_config)
    assert model_main(
        [
            "score",
            "--run-id",
            run_id,
            "--operational-db",
            str(operational_path),
            "--data-root",
            str(data_root),
        ]
    ) == 0
    assert score_csv.read_bytes() == first_csv
    capsys.readouterr()
    assert model_main(
        [
            "evaluate",
            "--run-id",
            run_id,
            "--operational-db",
            str(operational_path),
            "--private-db",
            str(private_path),
            "--data-root",
            str(data_root),
        ]
    ) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["overall"]["sample_count"] == score_summary["score_count"]
    assert report["overall"]["pr_auc"] is not None
    assert set(report["overall"]["coverage_by_class"]) == {"legit", "fraud"}
    assert report["as_of_time"] == "2026-03-02T17:00:00.000000Z"
    assert report["label_policy_version"] == "mvp-h90-v1"
    legit_support = report["overall"]["coverage_by_class"]["legit"]
    assert legit_support["alpha"] == 0.005
    assert legit_support["calibration_sample_count"] == 128
    assert legit_support["required_calibration_sample_count"] == 199
    assert legit_support["insufficient_support"] is True
    fraud_support = report["overall"]["coverage_by_class"]["fraud"]
    assert fraud_support["alpha"] == 0.20
    assert fraud_support["calibration_sample_count"] == 24
    assert fraud_support["required_calibration_sample_count"] == 4
    assert fraud_support["insufficient_support"] is False
    assert "not a model result" in report["interpretation"]
    assert (model_directory / "reports" / "evaluation.json").is_file()

    connection = sqlite3.connect(operational_path)
    connection.row_factory = sqlite3.Row
    try:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(model_scores)")
        }
        assert not any("truth" in column.lower() for column in columns)
        scores = connection.execute(
            "SELECT * FROM model_scores ORDER BY episode_id, decision_at, txn_id"
        ).fetchall()
        assert len(scores) == score_summary["score_count"]
        assert all(row["event_at"] == row["decision_at"] for row in scores)
        assert all(row["available_at"] == row["decision_at"] for row in scores)
        assert all(row["decision_region"] in {"legit", "fraud", "review"} for row in scores)
        assert {row["episode_id"] for row in scores} == set(expected_counts)
        selected_episode = scores[0]["episode_id"]
        after_first_day = "2026-02-01T17:00:00.000000Z"
        selected = next(
            dict(row)
            for row in scores
            if row["episode_id"] == selected_episode and row["decision_at"] > after_first_day
        )
        selected_decision = selected["decision_at"]
        selected_transaction = connection.execute(
            "SELECT * FROM transactions WHERE txn_id = ? AND episode_id = ?",
            (selected["txn_id"], selected_episode),
        ).fetchone()
        assert selected_transaction["created_at"] == selected_decision

        visible_scores = read_as_of(
            connection,
            "model_scores",
            episode_id=selected_episode,
            t0=T0,
            as_of_time=selected_decision,
        )
        assert selected["txn_id"] in {row["txn_id"] for row in visible_scores}
        assert all(row["episode_id"] == selected_episode for row in visible_scores)

        later = "2026-03-01T00:00:00.000000Z"
        assert selected_decision < later
        insert_operational_record(
            connection,
            "transactions",
            {
                "txn_id": f"txn-asof-later-{selected_episode}",
                "episode_id": selected_episode,
                "customer_id": selected_transaction["customer_id"],
                "merchant_id": selected_transaction["merchant_id"],
                "created_at": later,
                "event_at": later,
                "available_at": later,
                "amount": 950000,
                "channel": selected_transaction["channel"],
            },
        )
        other_episode = next(episode_id for episode_id in expected_counts if episode_id != selected_episode)
        selected_time = datetime.fromisoformat(selected_decision.replace("Z", "+00:00"))
        t0_time = datetime.fromisoformat(T0.replace("Z", "+00:00"))
        other_event = max(
            selected_time - timedelta(minutes=1), t0_time + timedelta(microseconds=1)
        )
        assert other_event < selected_time
        other_event_at = other_event.astimezone(UTC).isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        )
        insert_operational_record(
            connection,
            "transactions",
            {
                "txn_id": f"txn-other-episode-{other_episode}",
                "episode_id": other_episode,
                "customer_id": selected_transaction["customer_id"],
                "merchant_id": selected_transaction["merchant_id"],
                "created_at": other_event_at,
                "event_at": other_event_at,
                "available_at": other_event_at,
                "amount": 1500000,
                "channel": selected_transaction["channel"],
            },
        )
        transaction_snapshot = read_as_of(
            connection,
            "transactions",
            episode_id=selected_episode,
            t0=T0,
            as_of_time=selected_decision,
        )
        assert f"txn-asof-later-{selected_episode}" not in {
            row["txn_id"] for row in transaction_snapshot
        }
        assert f"txn-other-episode-{other_episode}" not in {
            row["txn_id"] for row in transaction_snapshot
        }
        connection.commit()
    finally:
        connection.close()

    connection = sqlite3.connect(operational_path)
    connection.row_factory = sqlite3.Row
    try:
        point_in_time_transactions = read_as_of(
            connection,
            "transactions",
            episode_id=selected_episode,
            t0=T0,
            as_of_time=selected_decision,
        )
        assert f"txn-asof-later-{selected_episode}" not in {
            row["txn_id"] for row in point_in_time_transactions
        }
        assert f"txn-other-episode-{other_episode}" not in {
            row["txn_id"] for row in point_in_time_transactions
        }
        target = next(
            row for row in point_in_time_transactions if row["txn_id"] == selected["txn_id"]
        )
        matrix = build_score_feature_matrix(
            connection,
            [target],
            episode_id=selected_episode,
            t0=T0,
        )
        predictor = joblib.load(model_directory / "predictor.joblib")
        calibrator = joblib.load(model_directory / "calibrator.joblib")
        conformal = joblib.load(model_directory / "conformal.joblib")
        values = np.asarray(
            [[np.nan if value is None else float(value) for value in row] for row in matrix.values],
            dtype=np.float64,
        )
        raw_score = float(predictor.predict_proba(values)[0, 1])
        calibrated_prob = calibrator.predict((raw_score,))[0]
        conformal_set = predict_sets(conformal, (raw_score,))[0]
        assert raw_score == selected["raw_score"]
        assert calibrated_prob == selected["calibrated_prob"]
        assert json.dumps(list(conformal_set), separators=(",", ":")) == selected["conformal_set"]
        assert decision_for_set(conformal_set) == selected["decision_region"]
    finally:
        connection.close()


def test_evaluator_reports_insufficient_support_for_both_calibration_classes(
    insufficient_support_conformal,
) -> None:
    report = _metric_block(
        [
            {"txn_id": "legit-txn", "raw_score": 0.1, "calibrated_prob": 0.1},
            {"txn_id": "fraud-txn", "raw_score": 0.9, "calibrated_prob": 0.9},
        ],
        {"legit-txn": 0, "fraud-txn": 1},
        insufficient_support_conformal,
    )

    legit = report["coverage_by_class"]["legit"]
    fraud = report["coverage_by_class"]["fraud"]
    assert legit["alpha"] == 0.005
    assert fraud["alpha"] == 0.20
    assert legit["calibration_sample_count"] == 1
    assert fraud["calibration_sample_count"] == 1
    assert legit["insufficient_support"] is True
    assert fraud["insufficient_support"] is True

