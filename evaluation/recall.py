"""Detection recall summaries and private report writing for P3.4."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from evaluation.matcher import (
    MONITORING_FEATURE_TO_TRUTH_FEATURE,
    load_private_evaluation_data,
    match_incidents,
    normalize_as_of_time,
)


@dataclass(frozen=True)
class EvaluationResult:
    """Report data and its location inside the private evaluator directory."""

    report: dict[str, Any]
    report_path: Path


def _ratio(detected: int, denominator: int) -> float | None:
    return detected / denominator if denominator else None


def _build_report(
    *,
    matches: tuple[dict[str, Any], ...],
    truth_rows: tuple[dict[str, Any], ...],
    as_of_time: str,
) -> dict[str, Any]:
    detected_truth_ids = {
        truth_id
        for match in matches
        for truth_id in match["matched_truth_ids"]
    }
    required_truths = [
        row for row in truth_rows if row["requires_intervention"] == 1
    ]
    benign_truths = [
        row for row in truth_rows if row["requires_intervention"] == 0
    ]
    detected_required = sum(
        str(row["incident_truth_id"]) in detected_truth_ids for row in required_truths
    )

    scenario_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for truth in truth_rows:
        scenario_groups[(str(truth["cause"]), str(truth["template"]))].append(truth)
    by_scenario = []
    for (cause, template), rows in sorted(scenario_groups.items()):
        required = [row for row in rows if row["requires_intervention"] == 1]
        benign = [row for row in rows if row["requires_intervention"] == 0]
        detected = sum(
            str(row["incident_truth_id"]) in detected_truth_ids for row in required
        )
        missed = sorted(
            str(row["incident_truth_id"])
            for row in required
            if str(row["incident_truth_id"]) not in detected_truth_ids
        )
        by_scenario.append(
            {
                "cause": cause,
                "template": template,
                "detected": detected,
                "denominator": len(required),
                "missed_truth_ids": missed,
                "recall": _ratio(detected, len(required)),
                "benign_truth_count": len(benign),
            }
        )

    matched_incidents = [match for match in matches if match["status"] == "matched"]
    unmatched_incidents = [match for match in matches if match["status"] == "unmatched"]
    benign_truth_ids = {
        str(row["incident_truth_id"]) for row in benign_truths
    }
    benign_incident_ids = sorted(
        {
            str(match["incident_id"])
            for match in matched_incidents
            if benign_truth_ids.intersection(match["matched_truth_ids"])
        }
    )
    matched_incident_ids = {
        str(match["incident_id"]) for match in matched_incidents
    }
    unmatched_incident_ids = sorted(
        str(match["incident_id"]) for match in unmatched_incidents
    )

    return {
        "schema_version": 1,
        "as_of_time": as_of_time,
        "matching_scope": "round1_minimal_time_segment_feature",
        "deferred_to_round2": [
            "ambiguous_match classification",
            "full delayed-window effects",
        ],
        "feature_mapping": dict(sorted(MONITORING_FEATURE_TO_TRUTH_FEATURE.items())),
        "detection_recall": {
            "detected": detected_required,
            "denominator": len(required_truths),
            "recall": _ratio(detected_required, len(required_truths)),
            "missed_truth_ids": sorted(
                str(row["incident_truth_id"])
                for row in required_truths
                if str(row["incident_truth_id"]) not in detected_truth_ids
            ),
        },
        "by_scenario": by_scenario,
        "truth_counts": {
            "total": len(truth_rows),
            "requires_intervention": len(required_truths),
            "benign": len(benign_truths),
        },
        "incident_counts": {
            "evaluated": len(matches),
            "matched": len(matched_incident_ids),
            "unmatched": len(unmatched_incident_ids),
            "benign_matched": len(benign_incident_ids),
        },
        "unmatched_incident_ids": unmatched_incident_ids,
        "benign_incident_ids": benign_incident_ids,
        "incident_matches": [
            {
                **match,
                "matched_truth_ids": sorted(match["matched_truth_ids"]),
            }
            for match in matches
        ],
    }


def evaluate_detection_recall(
    incidents: Iterable[dict[str, Any]],
    *,
    private_db_path: str | Path,
    episode_ids: Iterable[str],
    as_of_time: str,
) -> EvaluationResult:
    """Match P3.3 output, calculate recall, and write the report on the private side.

    The selected episode list comes from the locked evaluation manifest rather
    than incident rows, preserving truth cases with no alert in the denominator.
    """
    cutoff = normalize_as_of_time(as_of_time)
    context = load_private_evaluation_data(
        private_db_path,
        episode_ids=episode_ids,
    )
    matches = match_incidents(
        incidents,
        context.truth_rows,
        episode_ids=context.episode_ids,
        episode_end_at=context.episode_end_at,
        as_of_time=cutoff,
    )
    report = _build_report(
        matches=matches,
        truth_rows=context.truth_rows,
        as_of_time=cutoff,
    )

    private_root = Path(private_db_path).expanduser().resolve().parent
    report_path = private_root / "reports" / "p3_4_detection_recall.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
        newline="",
    )
    return EvaluationResult(report=report, report_path=report_path)
