"""Private-side evaluation for P4 decisions."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
import json
from pathlib import Path
import sqlite3
from typing import Any

from evaluation.matcher import (
    EvaluationMatcherError,
    PrivateEvaluationData,
    configured_metric_window_days,
    load_private_evaluation_data,
    match_incidents,
    normalize_as_of_time,
)
from evaluation.recall import EvaluationResult
from simulator.schema import PRIVATE_TABLES
from triage.decisions import DecisionSchemaError, validate_decision_row


AUTO_ROUTING_ACTIONS = frozenset({"fraud_ops", "data_ticket"})
HUMAN_ROUTING_ACTION = "human_review"
ROUTING_ACTIONS = AUTO_ROUTING_ACTIONS | {HUMAN_ROUTING_ACTION}


class DecisionEvaluationError(ValueError):
    """Raised when decisions or selected private truth violate their contracts."""


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _datetime(value: str, *, field: str) -> datetime:
    normalized = normalize_as_of_time(value)
    try:
        return datetime.fromisoformat(normalized[:-1] + "+00:00")
    except ValueError as exc:  # normalize_as_of_time normally catches this first.
        raise DecisionEvaluationError(f"{field} is not a valid timestamp") from exc


def _json_array(value: Any, *, field: str, truth_id: str) -> tuple[str, ...]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise DecisionEvaluationError(
                f"{field} must be valid JSON for truth {truth_id!r}"
            ) from exc
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise DecisionEvaluationError(
            f"{field} must be an array of non-empty strings for truth {truth_id!r}"
        )
    return tuple(value)


def _validated_decisions(
    decisions: Iterable[Mapping[str, Any]], *, episode_ids: tuple[str, ...]
) -> tuple[dict[str, Any], ...]:
    selected_episode_ids = set(episode_ids)
    seen_decision_ids: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for index, source in enumerate(decisions):
        try:
            row = validate_decision_row(source)
        except DecisionSchemaError as exc:
            raise DecisionEvaluationError(f"invalid decision row {index}: {exc}") from exc
        decision_id = row["decision_id"]
        if decision_id in seen_decision_ids:
            raise DecisionEvaluationError(f"duplicate decision_id: {decision_id!r}")
        seen_decision_ids.add(decision_id)
        if row["episode_id"] not in selected_episode_ids:
            raise DecisionEvaluationError(
                f"decision {decision_id!r} is outside the selected episode set"
            )

        decision_at = normalize_as_of_time(row["decision_at"])
        scope = json.loads(row["scope_json"])
        scope_end = scope["end_at"]
        if scope_end is not None and normalize_as_of_time(scope_end) != decision_at:
            raise DecisionEvaluationError(
                f"decision {decision_id!r} scope end_at must equal decision_at"
            )
        scope_start = scope["start_at"]
        if scope_start is not None and scope_end is not None:
            if _datetime(scope_start, field="scope_json.start_at") > _datetime(
                scope_end, field="scope_json.end_at"
            ):
                raise DecisionEvaluationError(
                    f"decision {decision_id!r} scope start_at is after end_at"
                )
        row["decision_at"] = decision_at
        normalized.append(row)

    return tuple(
        sorted(
            normalized,
            key=lambda row: (row["episode_id"], row["decision_at"], row["decision_id"]),
        )
    )


def _selector_text(value: str | None, *, name: str) -> str | None:
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise DecisionEvaluationError(f"{name} selector must be non-empty text")
    return value


def _format_pairs(pairs: Iterable[tuple[str, str | None]]) -> str:
    ordered = sorted(pairs, key=lambda pair: (pair[0], pair[1] or ""))
    return ", ".join(
        f"(policy_version={policy!r}, model_slug={slug!r})" for policy, slug in ordered
    ) or "none"


def _select_decision_scope(
    decisions: tuple[dict[str, Any], ...],
    *,
    policy_version: str | None,
    model_slug: str | None,
) -> tuple[tuple[dict[str, Any], ...], dict[str, Any]]:
    """Keep one (policy_version, model_slug) pair so a report never mixes providers.

    Agent decisions give the candidate pairs; without agent decisions the candidates
    are the rule policy versions. Rule decisions carry NULL model_slug, so they follow
    the selected policy_version only.
    """
    policy_version = _selector_text(policy_version, name="policy_version")
    model_slug = _selector_text(model_slug, name="model_slug")
    agent_pairs = {
        (row["policy_version"], row["model_slug"])
        for row in decisions
        if row["source"] == "agent"
    }
    candidates = agent_pairs or {(row["policy_version"], None) for row in decisions}
    if policy_version is None and model_slug is None:
        if len(candidates) > 1:
            raise DecisionEvaluationError(
                f"operational decisions contain {len(candidates)} (policy_version, model_slug) "
                f"pairs: {_format_pairs(candidates)}; select exactly one with "
                "--policy-version and/or --model-slug"
            )
        selected = next(iter(candidates), (None, None))
    else:
        selector = ", ".join(
            f"{name}={value!r}"
            for name, value in (("policy_version", policy_version), ("model_slug", model_slug))
            if value is not None
        )
        matches = {
            pair
            for pair in candidates
            if (policy_version is None or pair[0] == policy_version)
            and (model_slug is None or pair[1] == model_slug)
        }
        if not matches:
            raise DecisionEvaluationError(
                f"no (policy_version, model_slug) pair matches {selector}; "
                f"available pairs: {_format_pairs(candidates)}"
            )
        if len(matches) > 1:
            raise DecisionEvaluationError(
                f"{selector} matches {len(matches)} pairs: {_format_pairs(matches)}; "
                "add --model-slug to select exactly one"
            )
        selected = next(iter(matches))
    selected_policy_version, selected_model_slug = selected
    kept = tuple(
        row
        for row in decisions
        if row["policy_version"] == selected_policy_version
        and (row["source"] == "rule" or row["model_slug"] == selected_model_slug)
    )
    selection = {
        "policy_version": selected_policy_version,
        "model_slug": selected_model_slug,
        "excluded_decision_count": len(decisions) - len(kept),
    }
    return kept, selection


def _scope_snapshots(decision: Mapping[str, Any]) -> tuple[dict[str, Any], ...] | None:
    """Represent the decision's explicit scope interval for the P3 matcher."""
    scope = json.loads(decision["scope_json"])
    feature = scope["feature"]
    segment = scope["segment"]
    start_at = scope["start_at"]
    end_at = scope["end_at"]
    if any(
        not isinstance(value, str) or not value.strip()
        for value in (feature, segment, start_at, end_at)
    ):
        return None

    common = {
        "incident_id": f"decision-scope:{decision['decision_id']}",
        "episode_id": decision["episode_id"],
        "feature": feature,
        "segment": segment,
        "opened_at": start_at,
    }
    if start_at == end_at:
        return (
            {
                **common,
                "event_at": start_at,
                "available_at": start_at,
                "alert_ids_json": '["scope-point"]',
            },
        )
    return (
        {
            **common,
            "event_at": start_at,
            "available_at": start_at,
            "alert_ids_json": '["scope-start"]',
        },
        {
            **common,
            "event_at": end_at,
            "available_at": end_at,
            "alert_ids_json": '["scope-start", "scope-end"]',
        },
    )


def _match_decisions(
    decisions: tuple[dict[str, Any], ...],
    *,
    context: PrivateEvaluationData,
) -> tuple[dict[str, Any], ...]:
    truth_by_episode: dict[str, tuple[dict[str, Any], ...]] = {
        episode_id: tuple(
            truth for truth in context.truth_rows if truth["episode_id"] == episode_id
        )
        for episode_id in context.episode_ids
    }
    matches: list[dict[str, Any]] = []
    for decision in decisions:
        match_record: dict[str, Any] = {
            "decision_id": decision["decision_id"],
            "incident_id": decision["incident_id"],
            "episode_id": decision["episode_id"],
            "decision_at": decision["decision_at"],
            "status": "unmatched",
            "matched_truth_ids": [],
            "candidate_truth_ids": [],
        }
        snapshots = _scope_snapshots(decision)
        if snapshots is not None:
            try:
                match = match_incidents(
                    snapshots,
                    truth_by_episode[decision["episode_id"]],
                    episode_ids=context.episode_ids,
                    episode_end_at=context.episode_end_at,
                    as_of_time=decision["decision_at"],
                )[0]
            except EvaluationMatcherError as exc:
                raise DecisionEvaluationError(
                    f"could not match decision {decision['decision_id']!r}: {exc}"
                ) from exc
            match_record["status"] = match["status"]
            match_record["matched_truth_ids"] = sorted(match["matched_truth_ids"])
            match_record["candidate_truth_ids"] = sorted(match["candidate_truth_ids"])
        matches.append(match_record)
    return tuple(matches)


def _build_report(
    *,
    decisions: tuple[dict[str, Any], ...],
    matches: tuple[dict[str, Any], ...],
    truth_rows: tuple[dict[str, Any], ...],
    episode_ids: tuple[str, ...],
    selection: dict[str, Any],
) -> dict[str, Any]:
    truth_by_id = {str(row["incident_truth_id"]): row for row in truth_rows}
    required_truth_ids = {
        str(row["incident_truth_id"])
        for row in truth_rows
        if row["requires_intervention"] == 1
    }
    matched_by_decision = {match["decision_id"]: match for match in matches}

    timely_truth_ids: set[str] = set()
    auto_routed_truth_ids: set[str] = set()
    human_review_truth_ids: set[str] = set()
    unsafe_truth_ids: set[str] = set()
    triaged_incident_ids: set[str] = set()
    dangerous_incident_ids: set[str] = set()
    unmatched_closures: list[str] = []
    ambiguous_closures: list[str] = []

    for decision in decisions:
        match = matched_by_decision[decision["decision_id"]]
        action = decision["action"]
        if action == "close" and match["status"] == "unmatched":
            unmatched_closures.append(decision["decision_id"])
        elif action == "close" and match["status"] == "ambiguous_match":
            ambiguous_closures.append(decision["decision_id"])

        if match["status"] != "matched":
            continue

        decision_at = _datetime(decision["decision_at"], field="decision_at")
        for truth_id in match["matched_truth_ids"]:
            truth = truth_by_id[truth_id]
            if truth["requires_intervention"] != 1:
                continue
            incident_id = str(decision["incident_id"])
            triaged_incident_ids.add(incident_id)
            if action == "close":
                unsafe_truth_ids.add(truth_id)
                dangerous_incident_ids.add(incident_id)

            truth_start = _datetime(
                truth["intervention_start_at"],
                field=f"truth {truth_id!r} intervention_start_at",
            )
            truth_deadline = _datetime(
                truth["deadline_at"], field=f"truth {truth_id!r} deadline_at"
            )
            if truth_deadline < truth_start:
                raise DecisionEvaluationError(
                    f"truth {truth_id!r} deadline_at precedes intervention_start_at"
                )
            accepted_actions = set(
                _json_array(
                    truth["accepted_actions_json"],
                    field="accepted_actions_json",
                    truth_id=truth_id,
                )
            )
            timely = (
                action in ROUTING_ACTIONS
                and action in accepted_actions
                and truth_start <= decision_at <= truth_deadline
            )
            if not timely:
                continue
            timely_truth_ids.add(truth_id)
            if action in AUTO_ROUTING_ACTIONS:
                auto_routed_truth_ids.add(truth_id)
            elif action == HUMAN_ROUTING_ACTION:
                human_review_truth_ids.add(truth_id)

    lower_bound_observed = bool(unmatched_closures or ambiguous_closures)
    matched_count = sum(match["status"] == "matched" for match in matches)
    unmatched_count = sum(match["status"] == "unmatched" for match in matches)
    ambiguous_count = sum(match["status"] == "ambiguous_match" for match in matches)

    def routing_bucket(routed_ids: set[str]) -> dict[str, Any]:
        return {
            "routed": len(routed_ids),
            "denominator": len(required_truth_ids),
            "recall": _ratio(len(routed_ids), len(required_truth_ids)),
            "truth_ids": sorted(routed_ids),
        }

    return {
        "schema_version": 2,
        "matching_scope": "decision_scope_effect_window_plus_metric_window_segment_feature",
        "metric_window_days": configured_metric_window_days(),
        "episode_ids": list(episode_ids),
        "selection": selection,
        "decision_counts": {
            "total": len(decisions),
            "matched": matched_count,
            "unmatched": unmatched_count,
            "ambiguous": ambiguous_count,
            "close": sum(decision["action"] == "close" for decision in decisions),
            "unmatched_closures": len(unmatched_closures),
            "ambiguous_closures": len(ambiguous_closures),
            "unmatched_closure_decision_ids": sorted(unmatched_closures),
            "ambiguous_closure_decision_ids": sorted(ambiguous_closures),
        },
        "truth_counts": {
            "total": len(truth_rows),
            "requires_intervention": len(required_truth_ids),
            "benign": len(truth_rows) - len(required_truth_ids),
        },
        "timely_routing_recall": {
            **routing_bucket(timely_truth_ids),
            "missing_truth_ids": sorted(required_truth_ids - timely_truth_ids),
            "automatic_routing": routing_bucket(auto_routed_truth_ids),
            "human_review": routing_bucket(human_review_truth_ids),
        },
        "unsafe_truth_rate": {
            "unsafe": len(unsafe_truth_ids),
            "denominator": len(required_truth_ids),
            "rate": _ratio(len(unsafe_truth_ids), len(required_truth_ids)),
            "unsafe_truth_ids": sorted(unsafe_truth_ids),
            "lower_bound_observed": lower_bound_observed,
            "basis": "observed_lower_bound" if lower_bound_observed else "matched_truth_only",
        },
        "dangerous_closure_rate": {
            "dangerous_closures": len(dangerous_incident_ids),
            "denominator": len(triaged_incident_ids),
            "rate": _ratio(len(dangerous_incident_ids), len(triaged_incident_ids)),
            "dangerous_incident_ids": sorted(dangerous_incident_ids),
            "triaged_incident_ids": sorted(triaged_incident_ids),
        },
        "decision_matches": [
            {
                **match,
                "matched_truth_ids": sorted(match["matched_truth_ids"]),
                "candidate_truth_ids": sorted(match["candidate_truth_ids"]),
            }
            for match in matches
        ],
    }


def evaluate_decisions(
    decisions: Iterable[Mapping[str, Any]],
    *,
    private_db_path: str | Path,
    episode_ids: Iterable[str],
    policy_version: str | None = None,
    model_slug: str | None = None,
) -> EvaluationResult:
    """Score in-memory operational decisions and write a private-side report.

    Decisions are supplied by the caller; only the evaluator opens the private
    database, through the same read-only truth loader used by P3.4. Decisions that
    span several agent (policy_version, model_slug) pairs are refused unless the
    selector names exactly one of them.
    """
    context = load_private_evaluation_data(
        private_db_path,
        episode_ids=episode_ids,
    )
    normalized_decisions = _validated_decisions(
        decisions,
        episode_ids=context.episode_ids,
    )
    selected_decisions, selection = _select_decision_scope(
        normalized_decisions,
        policy_version=policy_version,
        model_slug=model_slug,
    )
    matches = _match_decisions(selected_decisions, context=context)
    report = _build_report(
        decisions=selected_decisions,
        matches=matches,
        truth_rows=context.truth_rows,
        episode_ids=context.episode_ids,
        selection=selection,
    )

    private_root = Path(private_db_path).expanduser().resolve().parent
    report_path = private_root / "reports" / "p4_5_decisions.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        + "\n",
        encoding="utf-8",
        newline="",
    )
    return EvaluationResult(report=report, report_path=report_path)


def evaluate_operational_decisions(
    operational_db_path: str | Path,
    *,
    private_db_path: str | Path,
    policy_version: str | None = None,
    model_slug: str | None = None,
) -> EvaluationResult:
    """Read persisted decisions from the operational DB, then score private truth.

    A DB whose agent decisions span several (policy_version, model_slug) pairs
    needs a selector for exactly one of them; otherwise the evaluator refuses.
    """
    operational_path = Path(operational_db_path).expanduser().resolve()
    private_path = Path(private_db_path).expanduser().resolve()
    if operational_path == private_path:
        raise DecisionEvaluationError(
            "operational decisions and private evaluator data must be separate databases"
        )
    if not operational_path.is_file():
        raise DecisionEvaluationError(
            f"operational database does not exist: {operational_path}"
        )

    connection = sqlite3.connect(f"{operational_path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        table_names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if not {"episodes", "decisions"}.issubset(table_names):
            raise DecisionEvaluationError(
                "operational database is missing episodes or decisions"
            )
        forbidden = table_names.intersection(PRIVATE_TABLES)
        if forbidden:
            raise DecisionEvaluationError(
                "operational database contains private evaluator tables"
            )
        episode_ids = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT episode_id FROM episodes WHERE episode_id <> 'base' ORDER BY episode_id"
            )
        )
        decisions = tuple(
            dict(row)
            for row in connection.execute(
                "SELECT * FROM decisions ORDER BY decision_at, episode_id, incident_id, decision_id"
            )
        )
    except sqlite3.Error as exc:
        raise DecisionEvaluationError(
            "could not read operational decisions in read-only mode"
        ) from exc
    finally:
        connection.close()

    return evaluate_decisions(
        decisions,
        private_db_path=private_path,
        episode_ids=episode_ids,
        policy_version=policy_version,
        model_slug=model_slug,
    )
