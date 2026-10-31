"""Private P5.0b development-only support report generation."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from typing import Any

from evaluation.matcher import (
    load_private_evaluation_data,
    match_incidents,
    normalize_as_of_time,
)
from payguard.identity import (
    assert_role_access,
    find_harness_root,
    policy_name_for_run,
)
from triage.gate import GateConfig
from triage.runbooks import load_triage_config


class DevelopmentSupportError(ValueError):
    """Raised when a P5.0b input violates its private development contract."""


@dataclass(frozen=True)
class DevelopmentSupportResult:
    report: dict[str, Any]
    report_path: Path


def _read_only_connection(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise DevelopmentSupportError(f"database does not exist: {path}")
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _rows_for_episode(
    connection: sqlite3.Connection,
    table: str,
    episode_id: str,
    as_of_time: str,
) -> list[dict[str, Any]]:
    if table not in {"daily_metrics", "alerts", "incidents", "decisions"}:
        raise DevelopmentSupportError(f"unsupported operational table: {table}")
    temporal_cutoff = "available_at <= ?"
    if table == "decisions":
        temporal_cutoff += " AND decision_at <= ?"
    query = (
        f"SELECT * FROM {table} WHERE episode_id = ? AND {temporal_cutoff} "
        "ORDER BY available_at, event_at, rowid"
    )
    params: tuple[Any, ...] = (episode_id, as_of_time)
    if table == "decisions":
        params = (episode_id, as_of_time, as_of_time)
    try:
        return [dict(row) for row in connection.execute(query, params).fetchall()]
    except sqlite3.Error as exc:
        raise DevelopmentSupportError(
            f"could not read development {table} for {episode_id!r}: {exc}"
        ) from exc


def _json_object(value: Any, *, field: str, episode_id: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError as exc:
        raise DevelopmentSupportError(
            f"{field} is invalid JSON in episode {episode_id!r}"
        ) from exc
    if not isinstance(parsed, dict):
        raise DevelopmentSupportError(
            f"{field} must be a JSON object in episode {episode_id!r}"
        )
    return parsed


def _json_array(value: Any, *, field: str, episode_id: str) -> list[Any]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError as exc:
        raise DevelopmentSupportError(
            f"{field} is invalid JSON in episode {episode_id!r}"
        ) from exc
    if not isinstance(parsed, list):
        raise DevelopmentSupportError(
            f"{field} must be a JSON array in episode {episode_id!r}"
        )
    return parsed


def _checklist_results(decision: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    raw = _json_object(
        decision.get("checklist_results_json"),
        field="checklist_results_json",
        episode_id=str(decision.get("episode_id")),
    )
    result: dict[str, dict[str, Any]] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not isinstance(value, dict):
            raise DevelopmentSupportError(
                f"decision checklist results are invalid for {decision.get('decision_id')!r}"
            )
        result[name] = value
    return result


def _has_forced_human_condition(
    decision: Mapping[str, Any], checklists: Mapping[str, Mapping[str, Any]]
) -> bool:
    if decision.get("evidence_status") == "conflicting":
        return True
    for result in checklists.values():
        if result.get("requires_human_review") is True or result.get("status") == "conflicting":
            return True

    applicable_runbooks = {
        name
        for name in ("seasonal_calendar", "unit_or_null_change")
        if name in checklists
        and checklists[name].get("status") != "not_applicable"
        and checklists[name].get("scope_match") is True
    }
    runbook = decision.get("runbook")
    if runbook in {"seasonal_calendar", "unit_or_null_change"}:
        applicable_runbooks.add(str(runbook))
    fraud_signal = checklists.get("fraud_signal")
    if (
        applicable_runbooks
        and fraud_signal is not None
        and fraud_signal.get("status") == "pass"
        and fraud_signal.get("scope_match") is True
    ):
        return True

    reason = decision.get("reason")
    return isinstance(reason, str) and "requires human review" in reason.casefold()


def _seasonal_check_passes(checklists: Mapping[str, Mapping[str, Any]]) -> bool:
    result = checklists.get("seasonal_calendar")
    return bool(
        result
        and result.get("status") == "pass"
        and result.get("scope_match") is True
        and result.get("window_complete") is True
    )


def _window_completeness(
    metric_rows: Sequence[Mapping[str, Any]], decisions: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    metrics_by_name: dict[str, Counter[str]] = defaultdict(Counter)
    for row in metric_rows:
        flags = _json_array(
            row.get("flags"),
            field="daily_metrics.flags",
            episode_id=str(row.get("episode_id")),
        )
        if any(not isinstance(flag, str) for flag in flags):
            raise DevelopmentSupportError(
                f"daily_metrics.flags contains a non-text value in {row.get('episode_id')!r}"
            )
        incomplete = "window_incomplete" in flags or any(
            flag.startswith("window_censored_samples=") for flag in flags
        )
        key = f"{row.get('metric_id')}:{row.get('feature')}"
        metrics_by_name[key]["incomplete" if incomplete else "complete"] += 1

    checklist_windows: dict[str, Counter[str]] = defaultdict(Counter)
    for decision in decisions:
        for name, result in _checklist_results(decision).items():
            if "window_complete" not in result:
                checklist_windows[name]["unknown"] += 1
            elif result["window_complete"] is True:
                checklist_windows[name]["complete"] += 1
            elif result["window_complete"] is False:
                checklist_windows[name]["incomplete"] += 1
            else:
                raise DevelopmentSupportError(
                    f"checklist {name!r} has a non-boolean window_complete value "
                    f"in decision {decision.get('decision_id')!r}"
                )

    metric_complete = sum(counts["complete"] for counts in metrics_by_name.values())
    metric_incomplete = sum(counts["incomplete"] for counts in metrics_by_name.values())
    return {
        "daily_metric_snapshots": {
            "evaluated": metric_complete + metric_incomplete,
            "complete": metric_complete,
            "incomplete": metric_incomplete,
            "by_metric": [
                {
                    "metric_feature": name,
                    "complete": counts["complete"],
                    "incomplete": counts["incomplete"],
                }
                for name, counts in sorted(metrics_by_name.items())
            ],
        },
        "triage_checklists": [
            {
                "checklist": name,
                "complete": counts["complete"],
                "incomplete": counts["incomplete"],
                "unknown": counts["unknown"],
            }
            for name, counts in sorted(checklist_windows.items())
        ],
    }


def _latest_incident_alert_ids(
    incident_rows: Sequence[Mapping[str, Any]],
) -> dict[str, tuple[str, ...]]:
    rows_by_incident: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in incident_rows:
        rows_by_incident[str(row["incident_id"])].append(row)
    result: dict[str, tuple[str, ...]] = {}
    for incident_id, rows in rows_by_incident.items():
        latest = max(
            rows,
            key=lambda row: (str(row["event_at"]), str(row["available_at"])),
        )
        alert_ids = _json_array(
            latest.get("alert_ids_json"),
            field="incidents.alert_ids_json",
            episode_id=str(latest.get("episode_id")),
        )
        if any(not isinstance(alert_id, str) or not alert_id.strip() for alert_id in alert_ids):
            raise DevelopmentSupportError(
                f"incident {incident_id!r} has an invalid alert ID"
            )
        result[incident_id] = tuple(alert_ids)
    return result


def _truth_support_rows(
    truth_rows: Sequence[Mapping[str, Any]],
    matches: Sequence[Mapping[str, Any]],
    decisions: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    matches_by_incident = {str(row["incident_id"]): row for row in matches}
    runbooks_by_truth: dict[str, set[str]] = defaultdict(set)
    matched_truth_ids: set[str] = set()
    for match in matches:
        if match["status"] == "matched":
            matched_truth_ids.update(str(item) for item in match["matched_truth_ids"])
    for decision in decisions:
        if decision.get("source") != "rule" or not decision.get("runbook"):
            continue
        match = matches_by_incident.get(str(decision["incident_id"]))
        if match is None or match["status"] != "matched":
            continue
        for truth_id in match["matched_truth_ids"]:
            runbooks_by_truth[str(truth_id)].add(str(decision["runbook"]))

    grouped: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for truth in truth_rows:
        grouped[
            (
                str(truth["episode_id"]),
                str(truth["cause"]),
                str(truth["template"]),
            )
        ].append(truth)
    result: list[dict[str, Any]] = []
    for (episode_id, cause, template), rows in sorted(grouped.items()):
        ids = {str(row["incident_truth_id"]) for row in rows}
        benign = {
            str(row["incident_truth_id"])
            for row in rows
            if int(row["requires_intervention"]) == 0
        }
        covered = {
            str(row["incident_truth_id"])
            for row in rows
            if int(row["rule_covered"]) == 1
        }
        result.append(
            {
                "episode_id": episode_id,
                "cause": cause,
                "template": template,
                "unique_truths": len(ids),
                "requires_intervention": sum(
                    int(row["requires_intervention"]) == 1 for row in rows
                ),
                "benign": len(benign),
                "matched_truths": len(ids & matched_truth_ids),
                "rule_covered_truths": len(covered),
                "matched_runbooks": sorted(
                    {
                        runbook
                        for truth_id in ids
                        for runbook in runbooks_by_truth.get(truth_id, set())
                    }
                ),
            }
        )
    return result


def _runbook_coverage(
    matches: Sequence[Mapping[str, Any]], decisions: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    matches_by_incident = {str(row["incident_id"]): row for row in matches}
    incident_ids_by_runbook: dict[str, set[str]] = defaultdict(set)
    truth_ids_by_runbook: dict[str, set[str]] = defaultdict(set)
    for decision in decisions:
        runbook = decision.get("runbook")
        if decision.get("source") != "rule" or not isinstance(runbook, str) or not runbook.strip():
            continue
        incident_id = str(decision["incident_id"])
        incident_ids_by_runbook[runbook].add(incident_id)
        match = matches_by_incident.get(incident_id)
        if match is not None and match["status"] == "matched":
            truth_ids_by_runbook[runbook].update(
                str(truth_id) for truth_id in match["matched_truth_ids"]
            )
    return [
        {
            "runbook": runbook,
            "incident_count": len(incident_ids_by_runbook[runbook]),
            "incident_ids": sorted(incident_ids_by_runbook[runbook]),
            "matched_truth_ids": sorted(truth_ids_by_runbook[runbook]),
        }
        for runbook in sorted(incident_ids_by_runbook)
    ]


def _agent_and_close_support(
    decisions: Sequence[Mapping[str, Any]],
    episode_ids: Sequence[str],
) -> dict[str, Any]:
    try:
        policy = GateConfig.from_mapping(load_triage_config())
    except (TypeError, ValueError, KeyError) as exc:
        raise DevelopmentSupportError(f"could not load triage close policy: {exc}") from exc

    agent_decisions = [row for row in decisions if row.get("source") == "agent"]
    agent_incidents = {str(row["incident_id"]) for row in agent_decisions}
    agent_incidents_by_episode: dict[str, set[str]] = defaultdict(set)
    agent_rows_by_episode: Counter[str] = Counter()
    for row in agent_decisions:
        episode_id = str(row["episode_id"])
        agent_incidents_by_episode[episode_id].add(str(row["incident_id"]))
        agent_rows_by_episode[episode_id] += 1

    rule_path_incidents: set[str] = set()
    agent_path_incidents: set[str] = set()
    authorized_agent_close_incidents: set[str] = set()
    uq_conditions_1_to_3: set[str] = set()
    for decision in decisions:
        source = decision.get("source")
        if source not in {"rule", "agent"}:
            continue
        checklists = _checklist_results(decision)
        forced_human = _has_forced_human_condition(decision, checklists)
        seasonal_pass = _seasonal_check_passes(checklists)
        evidence_sufficient = decision.get("evidence_status") == "sufficient"
        incident_id = str(decision["incident_id"])
        if source == "agent" and seasonal_pass and not forced_human:
            uq_conditions_1_to_3.add(incident_id)
        is_close_candidate = (
            seasonal_pass
            and not forced_human
            and evidence_sufficient
            and decision.get("cause") == "seasonal"
            and decision.get("runbook") == "seasonal_calendar"
        )
        if is_close_candidate and source == "rule":
            rule_path_incidents.add(incident_id)
        elif is_close_candidate and source == "agent":
            agent_path_incidents.add(incident_id)
            score = decision.get("close_score")
            if (
                not policy.agent_close_disabled
                and policy.tau_agent is not None
                and isinstance(score, (int, float))
                and not isinstance(score, bool)
                and score >= float(policy.tau_agent)
            ):
                authorized_agent_close_incidents.add(incident_id)

    rule_close_enabled = bool(policy.close_enabled.get("seasonal_calendar", False))
    agent_close_allowed = not policy.agent_close_disabled and policy.tau_agent is not None
    return {
        "incidents_reaching_agent": len(agent_incidents),
        "agent_decision_rows": len(agent_decisions),
        "by_episode": [
            {
                "episode_id": episode_id,
                "incidents_reaching_agent": len(agent_incidents_by_episode[episode_id]),
                "agent_decision_rows": agent_rows_by_episode[episode_id],
            }
            for episode_id in sorted(episode_ids)
        ],
        "close_eligibility": {
            "rule_path": {
                "checklist_eligible_incidents": len(rule_path_incidents),
                "close_enabled": rule_close_enabled,
                "authorized_to_close_now": len(rule_path_incidents) if rule_close_enabled else 0,
            },
            "agent_path": {
                "checklist_eligible_incidents": len(agent_path_incidents),
                "agent_close_disabled": policy.agent_close_disabled,
                "tau_agent": policy.tau_agent,
                "authorized_to_close_now": len(authorized_agent_close_incidents),
            },
        },
        "uq_conditions": {
            "conditions_1_to_3_incidents": len(uq_conditions_1_to_3),
            "condition_4_agent_close_allowed": agent_close_allowed,
            "conditions_1_to_4_incidents": (
                len(uq_conditions_1_to_3) if agent_close_allowed else 0
            ),
            "agent_close_disabled": policy.agent_close_disabled,
        },
    }


def build_development_support_report(
    *,
    operational_db_path: str | Path,
    private_db_path: str | Path,
    episode_ids: Sequence[str],
    as_of_by_episode: Mapping[str, str],
    policy_name: str,
) -> dict[str, Any]:
    """Build the P5.0b report from development operational rows and private truth."""
    selected_ids = tuple(sorted(set(episode_ids)))
    if not selected_ids or any(not item or item == "base" for item in selected_ids):
        raise DevelopmentSupportError("episode_ids must contain development episode IDs")
    if set(as_of_by_episode) != set(selected_ids):
        raise DevelopmentSupportError("as_of_by_episode must cover exactly the selected episodes")
    canonical_as_of = {
        episode_id: normalize_as_of_time(as_of_by_episode[episode_id])
        for episode_id in selected_ids
    }
    overall_as_of = max(canonical_as_of.values())

    private_path = Path(private_db_path).expanduser().resolve()
    private_connection = _read_only_connection(private_path)
    try:
        placeholders = ", ".join("?" for _ in selected_ids)
        rows = private_connection.execute(
            f"SELECT episode_id, role FROM episode_manifest WHERE episode_id IN ({placeholders}) "
            "ORDER BY episode_id",
            selected_ids,
        ).fetchall()
    except sqlite3.Error as exc:
        raise DevelopmentSupportError(f"could not validate development episode roles: {exc}") from exc
    finally:
        private_connection.close()
    if {str(row["episode_id"]) for row in rows} != set(selected_ids):
        raise DevelopmentSupportError("private manifest is missing a selected development episode")
    if any(row["role"] != "development" for row in rows):
        raise DevelopmentSupportError("P5.0b refuses any non-development episode")

    context = load_private_evaluation_data(private_path, episode_ids=selected_ids)
    operational_path = Path(operational_db_path).expanduser().resolve()
    operational_connection = _read_only_connection(operational_path)
    try:
        metrics: list[dict[str, Any]] = []
        alerts: list[dict[str, Any]] = []
        incidents: list[dict[str, Any]] = []
        decisions: list[dict[str, Any]] = []
        for episode_id in selected_ids:
            cutoff = canonical_as_of[episode_id]
            metrics.extend(_rows_for_episode(operational_connection, "daily_metrics", episode_id, cutoff))
            alerts.extend(_rows_for_episode(operational_connection, "alerts", episode_id, cutoff))
            incidents.extend(_rows_for_episode(operational_connection, "incidents", episode_id, cutoff))
            decisions.extend(_rows_for_episode(operational_connection, "decisions", episode_id, cutoff))
    finally:
        operational_connection.close()

    matches = match_incidents(
        incidents,
        context.truth_rows,
        episode_ids=selected_ids,
        episode_end_at=context.episode_end_at,
        as_of_time=overall_as_of,
        metric_window_days=context.metric_window_days,
    )
    match_counts = Counter(str(match["status"]) for match in matches)
    matches_by_episode: dict[str, Counter[str]] = defaultdict(Counter)
    for match in matches:
        matches_by_episode[str(match["episode_id"])][str(match["status"])] += 1

    truth_rows = list(context.truth_rows)
    truth_counts = Counter(
        "requires_intervention" if int(row["requires_intervention"]) else "benign"
        for row in truth_rows
    )
    truth_by_episode_and_cause = _truth_support_rows(truth_rows, matches, decisions)
    runbook_coverage = _runbook_coverage(matches, decisions)
    rule_covered_count = sum(int(row["rule_covered"]) == 1 for row in truth_rows)

    matches_by_incident = {str(row["incident_id"]): row for row in matches}
    incident_alert_ids = _latest_incident_alert_ids(incidents)
    unmatched_ids = sorted(
        incident_id
        for incident_id, match in matches_by_incident.items()
        if match["status"] == "unmatched"
    )
    alerts_in_unmatched_incidents = {
        alert_id
        for incident_id in unmatched_ids
        for alert_id in incident_alert_ids.get(incident_id, ())
    }
    all_alert_ids = {str(row["alert_id"]) for row in alerts}
    assigned_alert_ids = {
        alert_id for alert_ids in incident_alert_ids.values() for alert_id in alert_ids
    }
    unassigned_alert_ids = all_alert_ids - assigned_alert_ids
    unmatched_alert_ids = sorted(
        alerts_in_unmatched_incidents | unassigned_alert_ids
    )
    truth_by_id = {str(row["incident_truth_id"]): row for row in truth_rows}
    benign_truth_ids = {
        truth_id
        for truth_id, row in truth_by_id.items()
        if int(row["requires_intervention"]) == 0
    }
    seasonal_truth_ids = {
        truth_id
        for truth_id, row in truth_by_id.items()
        if int(row["requires_intervention"]) == 0 and row["cause"] == "seasonal"
    }
    benign_matched_incidents = [
        match
        for match in matches
        if match["status"] == "matched"
        and match["matched_truth_ids"]
        and all(str(truth_id) in benign_truth_ids for truth_id in match["matched_truth_ids"])
    ]
    seasonal_benign_matched_incidents = [
        match
        for match in benign_matched_incidents
        if any(str(truth_id) in seasonal_truth_ids for truth_id in match["matched_truth_ids"])
    ]
    seasonal_benign_truths_matched = {
        str(truth_id)
        for match in seasonal_benign_matched_incidents
        for truth_id in match["matched_truth_ids"]
        if str(truth_id) in seasonal_truth_ids
    }
    total_truths = len(truth_rows)
    truth_counts_by_episode = []
    for episode_id in selected_ids:
        episode_truths = [row for row in truth_rows if row["episode_id"] == episode_id]
        truth_counts_by_episode.append(
            {
                "episode_id": episode_id,
                "unique_truths": len(episode_truths),
                "requires_intervention": sum(
                    int(row["requires_intervention"]) == 1 for row in episode_truths
                ),
                "benign": sum(
                    int(row["requires_intervention"]) == 0 for row in episode_truths
                ),
                "rule_covered_truths": sum(
                    int(row["rule_covered"]) == 1 for row in episode_truths
                ),
            }
        )
    report = {
        "schema_version": 1,
        "report_type": "p5_0b_development_support",
        "purpose": "pilot_feasibility",
        "episode_role": "development",
        "policy_name": policy_name,
        "episode_ids": list(selected_ids),
        "as_of_time": overall_as_of,
        "as_of_time_by_episode": canonical_as_of,
        "truth_counts": {
            "total": total_truths,
            "requires_intervention": truth_counts["requires_intervention"],
            "benign": truth_counts["benign"],
            "rule_covered": rule_covered_count,
            "by_episode": truth_counts_by_episode,
            "by_episode_and_cause": truth_by_episode_and_cause,
        },
        "incident_counts": {
            "evaluated": len(matches),
            "matched": match_counts["matched"],
            "ambiguous_match": match_counts["ambiguous_match"],
            "unmatched": match_counts["unmatched"],
            "by_episode": [
                {
                    "episode_id": episode_id,
                    "evaluated": sum(matches_by_episode[episode_id].values()),
                    "matched": matches_by_episode[episode_id]["matched"],
                    "ambiguous_match": matches_by_episode[episode_id]["ambiguous_match"],
                    "unmatched": matches_by_episode[episode_id]["unmatched"],
                }
                for episode_id in selected_ids
            ],
        },
        "window_completeness": _window_completeness(metrics, decisions),
        "rule_coverage": {
            "rule_covered_truths": rule_covered_count,
            "truth_denominator": total_truths,
            "coverage": rule_covered_count / total_truths if total_truths else None,
            "matched_runbooks": runbook_coverage,
        },
        "agent_support": _agent_and_close_support(decisions, selected_ids),
        "background_workload": {
            "unmatched_incidents": len(unmatched_ids),
            "unmatched_incident_ids": unmatched_ids,
            "unmatched_alerts": len(unmatched_alert_ids),
            "unmatched_alert_ids": unmatched_alert_ids,
            "alerts_in_unmatched_incidents": len(alerts_in_unmatched_incidents),
            "unassigned_alerts": len(unassigned_alert_ids),
            "all_development_alerts": len(all_alert_ids),
        },
        "seasonal_benign_support": {
            "seasonal_benign_truths": len(seasonal_truth_ids),
            "matched_seasonal_benign_truths": len(seasonal_benign_truths_matched),
            "matched_seasonal_benign_incidents": len(seasonal_benign_matched_incidents),
            "status": "deferred_to_pilot_60_p1_6_round2",
        },
        "interpretation": {
            "scope": "pilot/feasibility; development episodes only",
            "episode_split_changed": False,
            "note": (
                "Seasonal benign support is deferred under the P3.7 decision; zero support in "
                "smoke does not estimate its pilot-60 prevalence."
            ),
        },
    }
    return report


def run_development_support(
    *, run_dir: str | Path, report_path: str | Path | None = None
) -> DevelopmentSupportResult:
    """Apply the P5.1 role guard and write a private development-only support report."""
    policy_run = Path(run_dir).expanduser().resolve()
    harness_root = find_harness_root(policy_run)
    if harness_root is None:
        raise DevelopmentSupportError("P5.0b support requires a P5.1 harness policy run")
    policy_name = policy_name_for_run(policy_run)
    if (
        policy_name is None
        or policy_run.parent.name != "policies"
        or policy_run.parent.parent != harness_root
        or policy_run.name != policy_name
    ):
        raise DevelopmentSupportError("P5.0b --run must identify a policy under harness policies/")

    operational_path = policy_run / "operational" / "observations.sqlite"
    try:
        operational_connection = _read_only_connection(operational_path)
        try:
            role_rows = operational_connection.execute(
                "SELECT episode_id FROM episodes WHERE role = 'development' "
                "AND episode_id <> 'base' ORDER BY episode_id"
            ).fetchall()
        finally:
            operational_connection.close()
    except sqlite3.Error as exc:
        raise DevelopmentSupportError(f"could not read development episode IDs: {exc}") from exc
    episode_ids = tuple(str(row["episode_id"]) for row in role_rows)
    if not episode_ids:
        raise DevelopmentSupportError("the selected policy run has no development episodes")

    assert_role_access(
        harness_root,
        operation="evaluation.support.development",
        role_episode_ids={"development": episode_ids},
        policy_name=policy_name,
    )

    private_path = harness_root / "base" / "private" / "evaluator.sqlite"
    private_context = load_private_evaluation_data(
        private_path,
        episode_ids=episode_ids,
    )
    as_of_by_episode = dict(private_context.episode_end_at)

    default_report_path = (
        harness_root
        / "base"
        / "private"
        / "reports"
        / "p5_0b"
        / policy_name
        / "development_support.json"
    )
    output_path = Path(report_path).expanduser().resolve() if report_path else default_report_path.resolve()
    private_reports = (harness_root / "base" / "private" / "reports").resolve()
    try:
        output_path.relative_to(private_reports)
    except ValueError as exc:
        raise DevelopmentSupportError(
            "P5.0b reports must stay under the shared private reports directory"
        ) from exc

    report = build_development_support_report(
        operational_db_path=operational_path,
        private_db_path=private_path,
        episode_ids=episode_ids,
        as_of_by_episode=as_of_by_episode,
        policy_name=policy_name,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
        newline="",
    )
    return DevelopmentSupportResult(report=report, report_path=output_path)


__all__ = [
    "DevelopmentSupportError",
    "DevelopmentSupportResult",
    "build_development_support_report",
    "run_development_support",
]
