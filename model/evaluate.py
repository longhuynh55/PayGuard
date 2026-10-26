"""P2.5 smoke scoring, operational score export, and private evaluation."""

from __future__ import annotations

import csv
from datetime import UTC, datetime, time, timedelta
import json
from pathlib import Path
import sqlite3
from typing import Any
from zoneinfo import ZoneInfo

import joblib
import numpy as np
from sklearn.metrics import average_precision_score

from model.conformal import (
    ConformalReport,
    FittedMondrianConformal,
    decision_for_set,
    evaluate_conformal,
    predict_sets,
)
from model.data import build_splits, load_model_config
from model.features import FEATURE_NAMES, build_feature_splits, build_score_feature_matrix
from model.train import artifact_directory_for_run, save_model_run, train_and_calibrate
from simulator.asof import read_as_of
from simulator.config import (
    EpisodeConfig,
    SimulatorConfig,
    load_episode_config,
    load_simulator_config,
)
from simulator.labels import label_truth_metadata_key
from simulator.storage import insert_operational_record, utc_timestamp


def _read_model_artifacts(model_directory: str | Path) -> tuple[Any, Any, FittedMondrianConformal, dict[str, Any]]:
    path = Path(model_directory)
    try:
        with (path / "metadata.json").open("r", encoding="utf-8") as stream:
            metadata = json.load(stream)
        predictor = joblib.load(path / "predictor.joblib")
        calibrator = joblib.load(path / "calibrator.joblib")
        conformal = joblib.load(path / "conformal.joblib")
    except (OSError, json.JSONDecodeError, FileNotFoundError) as exc:
        raise ValueError(f"could not load complete model artifacts from {path}: {exc}") from exc
    if metadata.get("feature_names") != list(FEATURE_NAMES):
        raise ValueError("model artifacts use a different feature schema")
    if not isinstance(conformal, FittedMondrianConformal):
        raise ValueError("model artifacts do not contain a Mondrian conformal model")
    return predictor, calibrator, conformal, metadata


def train_operational_model(
    operational: sqlite3.Connection,
    *,
    run_id: str,
    data_root: str | Path = "data",
    simulator_config_path: str | Path = "configs/simulator.yaml",
    model_config_path: str | Path = "configs/model.yaml",
    episode_id: str | None = None,
) -> tuple[Path, str]:
    """Run P2.1–P2.4 using operational data and save an untracked artifact."""
    simulator_config = load_simulator_config(simulator_config_path)
    model_config = load_model_config(model_config_path)
    registered = [
        (str(row[0]), str(row[1]))
        for row in operational.execute(
            "SELECT episode_id, role FROM episodes WHERE episode_id <> 'base' "
            "ORDER BY CASE role WHEN 'development' THEN 0 ELSE 1 END, episode_id"
        )
    ]
    if not registered:
        raise ValueError("training requires at least one registered non-base episode")
    if episode_id is None:
        episode_id = registered[0][0]
    elif episode_id not in {item[0] for item in registered}:
        raise ValueError(f"training episode is not registered: {episode_id!r}")

    splits = build_splits(
        operational,
        simulator_config,
        episode_id=episode_id,
        model_config=model_config,
    )
    features = build_feature_splits(operational, splits)
    model_run = train_and_calibrate(
        splits,
        features,
        config_path=model_config_path,
    )
    artifact_dir = artifact_directory_for_run(run_id, data_root=data_root)
    save_model_run(model_run, artifact_dir)
    reports_dir = artifact_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    with (reports_dir / "training_audit.json").open("w", encoding="utf-8") as stream:
        json.dump(
            {
                "run_id": run_id,
                "training_episode_id": episode_id,
                "split_audit": {
                    "episode_id": splits.audit.episode_id,
                    "as_of_time": splits.audit.as_of_time,
                    "horizon_days": splits.audit.horizon_days,
                    "label_policy_version": splits.audit.label_policy_version,
                    "splits": [
                        {
                            "name": split.name,
                            "candidate_count": split.candidate_count,
                            "sample_count": split.sample_count,
                            "positive_count": split.positive_count,
                            "negative_count": split.negative_count,
                            "pending_excluded_count": split.pending_excluded_count,
                            "under_horizon_excluded_count": split.under_horizon_excluded_count,
                        }
                        for split in splits.audit.splits
                    ],
                },
            },
            stream,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        stream.write("\n")
    return artifact_dir, splits.audit.format()


def _monitoring_window(
    simulator_config: SimulatorConfig,
    episode_config: EpisodeConfig,
) -> tuple[str, str]:
    timezone = ZoneInfo(simulator_config.timeline.business_timezone.value)
    t0_local = datetime.combine(
        simulator_config.timeline.t0_business_date.value,
        time.min,
        tzinfo=timezone,
    )
    t0 = t0_local.astimezone(UTC)
    end = t0 + timedelta(days=episode_config.protocol.duration_days.value)
    return utc_timestamp(t0), utc_timestamp(end)


def _feature_array(values: tuple[tuple[int | float | None, ...], ...]) -> np.ndarray:
    if not values:
        return np.empty((0, len(FEATURE_NAMES)), dtype=np.float64)
    return np.asarray(
        [[np.nan if value is None else float(value) for value in row] for row in values],
        dtype=np.float64,
    )


def _write_or_verify_scores(
    operational: sqlite3.Connection,
    score_rows: list[dict[str, Any]],
) -> None:
    """Insert deterministic scores and reject a conflicting rerun of a version."""
    operational.execute("SAVEPOINT p2_5_model_scores")
    try:
        for score in score_rows:
            existing = operational.execute(
                """
                SELECT txn_id, episode_id, decision_at, event_at, available_at,
                       raw_score, calibrated_prob, conformal_set, decision_region,
                       model_version, label_policy_version
                FROM model_scores
                WHERE txn_id = ? AND episode_id = ? AND model_version = ?
                """,
                (score["txn_id"], score["episode_id"], score["model_version"]),
            ).fetchone()
            if existing is None:
                insert_operational_record(operational, "model_scores", score)
                continue
            stored = dict(existing) if isinstance(existing, sqlite3.Row) else dict(
                zip(
                    (
                        "txn_id", "episode_id", "decision_at", "event_at", "available_at",
                        "raw_score", "calibrated_prob", "conformal_set", "decision_region",
                        "model_version", "label_policy_version",
                    ),
                    existing,
                    strict=True,
                )
            )
            encoded_set = json.dumps(score["conformal_set"], separators=(",", ":"))
            expected = {**score, "conformal_set": encoded_set}
            if stored != expected:
                raise ValueError(
                    "a model score already exists for this transaction and model version "
                    "with different values"
                )
        operational.execute("RELEASE SAVEPOINT p2_5_model_scores")
    except Exception:
        operational.execute("ROLLBACK TO SAVEPOINT p2_5_model_scores")
        operational.execute("RELEASE SAVEPOINT p2_5_model_scores")
        raise


def score_monitoring_episodes(
    operational: sqlite3.Connection,
    *,
    model_directory: str | Path,
    simulator_config_path: str | Path = "configs/simulator.yaml",
    episode_config_path: str | Path = "configs/episodes.yaml",
) -> list[dict[str, Any]]:
    """Score all registered episodes without opening or reading private data."""
    predictor, calibrator, conformal, metadata = _read_model_artifacts(model_directory)
    simulator_config = load_simulator_config(simulator_config_path)
    episode_config = load_episode_config(episode_config_path)
    t0, monitor_end = _monitoring_window(simulator_config, episode_config)
    episode_ids = [
        str(row[0])
        for row in operational.execute(
            "SELECT episode_id FROM episodes WHERE episode_id <> 'base' ORDER BY episode_id"
        )
    ]
    if not episode_ids:
        raise ValueError("scoring requires registered smoke episodes")

    score_rows: list[dict[str, Any]] = []
    for episode_id in episode_ids:
        candidates = read_as_of(
            operational,
            "transactions",
            episode_id=episode_id,
            t0=t0,
            as_of_time=monitor_end,
        )
        monitoring_transactions = [
            transaction
            for transaction in candidates
            if transaction["episode_id"] == episode_id
            and t0 <= transaction["created_at"] < monitor_end
        ]
        feature_matrix = build_score_feature_matrix(
            operational,
            monitoring_transactions,
            episode_id=episode_id,
            t0=t0,
        )
        raw_scores = tuple(
            float(value)
            for value in (
                predictor.predict_proba(_feature_array(feature_matrix.values))[:, 1]
                if feature_matrix.values
                else ()
            )
        )
        if not np.isfinite(raw_scores).all():
            raise ValueError("model produced non-finite monitoring scores")
        calibrated = calibrator.predict(raw_scores)
        conformal_sets = predict_sets(conformal, raw_scores)
        for example, raw_score, calibrated_prob, conformal_set in zip(
            feature_matrix.examples,
            raw_scores,
            calibrated,
            conformal_sets,
            strict=True,
        ):
            decision_at = utc_timestamp(example.event_at)
            score_rows.append(
                {
                    "txn_id": example.txn_id,
                    "episode_id": example.episode_id,
                    "decision_at": decision_at,
                    "event_at": decision_at,
                    "available_at": decision_at,
                    "raw_score": raw_score,
                    "calibrated_prob": calibrated_prob,
                    "conformal_set": list(conformal_set),
                    "decision_region": decision_for_set(conformal_set),
                    "model_version": metadata["model_version"],
                    "label_policy_version": metadata["label_policy_version"],
                }
            )

    score_rows.sort(key=lambda row: (row["episode_id"], row["decision_at"], row["txn_id"]))
    _write_or_verify_scores(operational, score_rows)
    _write_score_csv(Path(model_directory) / "model_scores.csv", score_rows)
    return score_rows


def _write_score_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = (
        "txn_id", "episode_id", "decision_at", "event_at", "available_at", "raw_score",
        "calibrated_prob", "conformal_set", "decision_region", "model_version",
        "label_policy_version",
    )
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    **row,
                    "conformal_set": json.dumps(row["conformal_set"], separators=(",", ":")),
                }
            )


def _truth_for_episode(private: sqlite3.Connection, episode_id: str) -> dict[str, int]:
    key = label_truth_metadata_key(episode_id)
    record = private.execute(
        "SELECT value_json FROM run_metadata WHERE key = ?", (key,)
    ).fetchone()
    if record is None:
        raise ValueError(f"private transaction truth is missing for episode {episode_id!r}")
    value_json = record[0]
    metadata = json.loads(value_json)
    if metadata.get("episode_id") != episode_id:
        raise ValueError(f"private transaction truth is assigned to the wrong episode {episode_id!r}")
    outcomes = metadata.get("transactions")
    if not isinstance(outcomes, list):
        raise ValueError(f"private transaction truth is malformed for episode {episode_id!r}")
    truth: dict[str, int] = {}
    for outcome in outcomes:
        if outcome.get("episode_id") != episode_id or outcome.get("true_label") not in {"fraud", "legit"}:
            raise ValueError(f"private transaction truth is malformed for episode {episode_id!r}")
        txn_id = outcome.get("txn_id")
        if not isinstance(txn_id, str) or txn_id in truth:
            raise ValueError(f"private transaction truth has a duplicate or missing ID in {episode_id!r}")
        truth[txn_id] = int(outcome["true_label"] == "fraud")
    return truth


def _coverage_dict(report: ConformalReport) -> dict[str, dict[str, int | float | bool | None]]:
    thresholds_by_class = {threshold.label: threshold for threshold in report.thresholds}
    return {
        label: {
            "covered_count": coverage.covered_count,
            "sample_count": coverage.sample_count,
            "rate": coverage.rate,
            "alpha": thresholds_by_class[label].alpha,
            "calibration_sample_count": thresholds_by_class[label].sample_count,
            "required_calibration_sample_count": (
                thresholds_by_class[label].required_calibration_count
            ),
            "insufficient_support": thresholds_by_class[label].insufficient_support,
        }
        for label, coverage in report.coverage_by_class.items()
    }


def _metric_block(
    rows: list[dict[str, Any]],
    truth_by_txn: dict[str, int],
    conformal: FittedMondrianConformal,
) -> dict[str, Any]:
    unknown = sorted({row["txn_id"] for row in rows} - truth_by_txn.keys())
    if unknown:
        raise ValueError(f"private truth does not cover scored transaction {unknown[0]!r}")
    targets = [truth_by_txn[row["txn_id"]] for row in rows]
    raw_scores = [float(row["raw_score"]) for row in rows]
    calibrated = [float(row["calibrated_prob"]) for row in rows]
    n_fraud = sum(targets)
    n_legit = len(targets) - n_fraud
    pr_auc = (
        float(average_precision_score(targets, calibrated))
        if n_fraud > 0 and n_legit > 0
        else None
    )
    conformal_report = evaluate_conformal(conformal, raw_scores, targets)
    return {
        "sample_count": len(rows),
        "fraud_count": n_fraud,
        "legit_count": n_legit,
        "pr_auc": pr_auc,
        "coverage_by_class": _coverage_dict(conformal_report),
    }


def evaluate_model_scores(
    operational: sqlite3.Connection,
    private: sqlite3.Connection,
    *,
    model_directory: str | Path,
    simulator_config_path: str | Path = "configs/simulator.yaml",
    episode_config_path: str | Path = "configs/episodes.yaml",
) -> dict[str, Any]:
    """Evaluate persisted scores against private simulator truth in a report only."""
    if operational is private:
        raise ValueError("evaluation requires separate operational and private connections")
    _, _, conformal, model_metadata = _read_model_artifacts(model_directory)
    simulator_config = load_simulator_config(simulator_config_path)
    episode_config = load_episode_config(episode_config_path)
    t0, monitor_end = _monitoring_window(simulator_config, episode_config)
    episode_ids = [
        str(row[0])
        for row in operational.execute(
            "SELECT episode_id FROM episodes WHERE episode_id <> 'base' ORDER BY episode_id"
        )
    ]
    if not episode_ids:
        raise ValueError("evaluation requires registered smoke episodes")

    episode_reports: list[dict[str, Any]] = []
    pooled_rows: list[dict[str, Any]] = []
    pooled_truth: dict[str, int] = {}
    for episode_id in episode_ids:
        visible_scores = read_as_of(
            operational,
            "model_scores",
            episode_id=episode_id,
            t0=t0,
            as_of_time=monitor_end,
        )
        rows = [
            row
            for row in visible_scores
            if row["episode_id"] == episode_id
            and row["model_version"] == model_metadata["model_version"]
            and t0 <= row["decision_at"] < monitor_end
        ]
        truth = _truth_for_episode(private, episode_id)
        metrics = _metric_block(rows, truth, conformal)
        episode_reports.append({"episode_id": episode_id, **metrics})
        pooled_rows.extend(rows)
        for row in rows:
            pooled_truth[row["txn_id"]] = truth[row["txn_id"]]

    report = {
        "run_id": Path(model_directory).parent.name,
        "as_of_time": monitor_end,
        "model_version": model_metadata["model_version"],
        "label_policy_version": model_metadata["label_policy_version"],
        "truth_basis": "private simulator transaction truth",
        "pr_auc_definition": "scikit-learn average_precision_score",
        "overall": _metric_block(pooled_rows, pooled_truth, conformal),
        "episodes": episode_reports,
        "interpretation": "Synthetic smoke check only; these metrics are not a model result or benchmark estimate.",
    }
    reports_dir = Path(model_directory) / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    with (reports_dir / "evaluation.json").open("w", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
    return report
