"""Focused, truth-free token measurement for the P4.6 round-one agent run."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, time, timedelta
import json
from pathlib import Path
import sqlite3
from typing import Any
from zoneinfo import ZoneInfo

from monitoring.incidents import OperationalDecisionSource, compute_incidents
from monitoring.store import persist_monitoring_rows
from simulator.asof import read_as_of
from simulator.config import load_simulator_config
from simulator.storage import StorageError, utc_timestamp
from triage.agent import AgentProposalProvider, OneStepAgent
from triage.decisions import persist_decisions
from triage.gate import GateConfig, evaluate_gate
from triage.runbooks import check_unit_or_null_change, load_triage_config
from triage.runner import (
    _ensure_provider_matches_run_history,
    _open_operational,
    _proposal_for_incident,
    _unit_or_null_rule_applies,
)


DESIGN_CALLS_PER_INCIDENT = 60


class MeasurementError(ValueError):
    """A safe P4.6 measurement precondition or execution error."""


def _run_timestamps(simulator_config) -> tuple[str, str]:
    timezone = ZoneInfo(simulator_config.timeline.business_timezone.value)
    t0 = utc_timestamp(
        datetime.combine(
            simulator_config.timeline.t0_business_date.value,
            time.min,
            tzinfo=timezone,
        )
    )
    cutoff = utc_timestamp(
        datetime.combine(
            simulator_config.timeline.end_business_date_exclusive.value - timedelta(days=1),
            time.max,
            tzinfo=timezone,
        )
    )
    return t0, cutoff


def _prepare_incident_snapshots(
    operational: sqlite3.Connection,
    *,
    episode_ids: Sequence[str],
    simulator_config,
    t0: str,
    catalog_cutoff: str,
) -> tuple[str, ...]:
    metric_rows: list[dict[str, Any]] = []
    for episode_id in episode_ids:
        metric_rows.extend(
            read_as_of(
                operational,
                "daily_metrics",
                episode_id=episode_id,
                t0=t0,
                as_of_time=catalog_cutoff,
            )
        )
    if not metric_rows:
        raise StorageError(
            "P3 daily_metrics are missing for development/calibration episodes; "
            "run monitoring metrics before triage measure"
        )
    if any(row["available_at"] > catalog_cutoff for row in metric_rows):
        raise StorageError("daily_metrics catalog contains a row unavailable in the run timeline")

    snapshot_times = tuple(sorted({row["as_of_time"] for row in metric_rows}))
    for as_of_time in snapshot_times:
        for episode_id in episode_ids:
            alerts = read_as_of(
                operational,
                "alerts",
                episode_id=episode_id,
                t0=t0,
                as_of_time=as_of_time,
            )
            decision_source = OperationalDecisionSource(
                operational=operational,
                t0=t0,
                as_of_time=as_of_time,
            )
            incident_result = compute_incidents(
                alerts,
                episode_id=episode_id,
                decision_source=decision_source,
            )
            current_rows = [
                row for row in incident_result.incidents if row["available_at"] == as_of_time
            ]
            persist_monitoring_rows(operational, "incidents", current_rows)
            # The SQL tool opens a separate read-only connection for this snapshot.
            operational.commit()
    return snapshot_times


def _agent_eligible_candidates(
    operational: sqlite3.Connection,
    *,
    episode_ids: Sequence[str],
    snapshot_times: Sequence[str],
    simulator_config,
    triage_config: Mapping[str, Any],
    t0: str,
) -> list[dict[str, Any]]:
    if not episode_ids or not snapshot_times:
        return []
    first_by_incident: dict[tuple[str, str], dict[str, Any]] = {}
    for as_of_time in sorted(snapshot_times):
        for episode_id in episode_ids:
            visible_rows = read_as_of(
                operational,
                "incidents",
                episode_id=episode_id,
                t0=t0,
                as_of_time=as_of_time,
            )
            for visible in visible_rows:
                key = (episode_id, visible["incident_id"])
                current = first_by_incident.get(key)
                if current is None or (
                    visible["available_at"], visible["event_at"]
                ) < (current["available_at"], current["event_at"]):
                    first_by_incident[key] = visible

    candidates: list[dict[str, Any]] = []
    for (episode_id, incident_id), incident in first_by_incident.items():
        first_as_of = incident["available_at"]
        first_snapshot = read_as_of(
            operational,
            "incidents",
            episode_id=episode_id,
            t0=t0,
            as_of_time=first_as_of,
        )
        incident = next(
            (
                row
                for row in first_snapshot
                if row["incident_id"] == incident_id
                and row["event_at"] == incident["event_at"]
                and row["available_at"] == first_as_of
            ),
            None,
        )
        if incident is None:
            continue
        if incident["status"] not in {"open", "recheck"}:
            continue
        checklist_results = check_unit_or_null_change(
            operational,
            incident,
            as_of_time=incident["available_at"],
            simulator_config=simulator_config,
            triage_config=triage_config,
        )
        if _unit_or_null_rule_applies(checklist_results):
            continue
        candidates.append(
            {"incident": incident, "checklist_results": checklist_results}
        )
    candidates.sort(
        key=lambda candidate: (
            candidate["incident"]["episode_id"],
            candidate["incident"]["available_at"],
            candidate["incident"]["incident_id"],
        )
    )
    return candidates


def _selection_entry(candidate: Mapping[str, Any]) -> dict[str, str]:
    incident = candidate["incident"]
    return {
        "episode_id": incident["episode_id"],
        "incident_id": incident["incident_id"],
        "as_of_time": incident["available_at"],
    }


def _write_report(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _totals(calls: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    total_latency_ms = sum(float(call["latency_ms"]) for call in calls)
    return {
        "call_count": len(calls),
        "input_tokens": sum(int(call["input_tokens"]) for call in calls),
        "output_tokens": sum(int(call["output_tokens"]) for call in calls),
        "total_tokens": sum(int(call["total_tokens"]) for call in calls),
        "cost_usd": round(sum(float(call["cost_usd"]) for call in calls), 12),
        "latency_ms": round(total_latency_ms, 3),
        "average_latency_ms": (
            round(total_latency_ms / len(calls), 3) if calls else 0.0
        ),
    }


def _refresh_report_totals(report: dict[str, Any]) -> None:
    calls = report["calls"]
    report["totals"] = _totals(calls)
    estimated_cost = None
    if calls:
        average_cost_per_call = report["totals"]["cost_usd"] / len(calls)
        estimated_cost = round(average_cost_per_call * DESIGN_CALLS_PER_INCIDENT, 12)
    report["extrapolation"] = {
        "label": "Estimate for the full design, not a measured benchmark result",
        "design_calls_per_agent_incident": DESIGN_CALLS_PER_INCIDENT,
        "observed_average_cost_per_call_usd": (
            round(report["totals"]["cost_usd"] / len(calls), 12) if calls else None
        ),
        "estimated_cost_per_agent_incident_usd": estimated_cost,
        "basis": "Approximately 60 calls per agent incident in MASTER_PLAN.md section 5.",
    }


def _incident_totals(
    selected: Mapping[str, str], calls: Sequence[Mapping[str, Any]], status: str
) -> dict[str, Any]:
    summary = _totals(calls)
    return {
        **selected,
        **summary,
        "status": status,
        "model_canonical_slugs": sorted(
            {str(call["model_canonical_slug"]) for call in calls}
        ),
    }


def _persist_agent_decision(
    operational: sqlite3.Connection,
    incident: Mapping[str, Any],
    checklist_results: Mapping[str, Mapping[str, Any]],
    *,
    as_of_time: str,
    provider: AgentProposalProvider,
    simulator_config,
    triage_config: Mapping[str, Any],
) -> dict[str, Any]:
    source, proposal = _proposal_for_incident(
        operational,
        incident,
        as_of_time=as_of_time,
        simulator_config=simulator_config,
        triage_config=triage_config,
        agent_provider=provider,
        checklist_results=checklist_results,
    )
    if source != "agent":
        raise StorageError("selected incident became rule-handled before agent measurement")

    tool_error = proposal.pop("_tool_error", False)
    tool_error_reason = proposal.pop("_tool_error_reason", None)
    usage = proposal.pop("_usage", None)
    model_slug = proposal.pop("_model_slug", provider.model_slug)
    if usage is not None and not isinstance(usage, Mapping):
        tool_error = True
        tool_error_reason = "Agent returned invalid LLM usage metadata."
        usage = None
    if model_slug is not None and not isinstance(model_slug, str):
        tool_error = True
        tool_error_reason = "Agent returned an invalid model slug."
        model_slug = None
    decision = evaluate_gate(
        incident,
        proposal,
        source="agent",
        expected_episode_id=incident["episode_id"],
        as_of_time=as_of_time,
        config=GateConfig.from_mapping(triage_config),
        tool_error=tool_error if isinstance(tool_error, bool) else True,
        tool_error_reason=tool_error_reason,
        usage=usage,
        model_slug=model_slug,
    )
    persist_decisions(operational, (decision,))
    operational.commit()
    return decision


def measure_triage(
    run_dir: str | Path,
    *,
    agent_provider: AgentProposalProvider,
    provider_name: str,
    incidents: int = 3,
    out: str | Path,
    simulator_config_path: str | Path = "configs/simulator.yaml",
    triage_config_path: str | Path = "configs/triage.yaml",
) -> dict[str, Any]:
    """Select and measure a fixed development-first set of one-step incidents."""
    if isinstance(incidents, bool) or not isinstance(incidents, int) or incidents < 1:
        raise MeasurementError("--incidents must be a positive integer")
    if provider_name not in {"fake", "openrouter"}:
        raise MeasurementError("measure supports only fake or openrouter providers")
    if not isinstance(agent_provider, OneStepAgent):
        raise MeasurementError("measure requires the existing OneStepAgent provider")

    root = Path(run_dir).expanduser().resolve()
    operational_db = root / "operational" / "observations.sqlite"
    report_path = Path(out).expanduser().resolve()
    simulator_config = load_simulator_config(simulator_config_path)
    triage_config = load_triage_config(triage_config_path)
    t0, catalog_cutoff = _run_timestamps(simulator_config)

    with _open_operational(operational_db) as operational:
        # Retain the P4.4b provider mismatch guard, then require the stricter
        # clean-agent-history condition for a first measurement run.
        _ensure_provider_matches_run_history(
            operational, model_slug=agent_provider.model_slug
        )
        prior_agent_decisions = operational.execute(
            "SELECT COUNT(*) FROM decisions WHERE source='agent'"
        ).fetchone()[0]
        if prior_agent_decisions:
            raise StorageError(
                "operational database already contains agent decisions; "
                "use a fresh copy of the run directory for triage measure"
            )

        episode_rows = operational.execute(
            "SELECT episode_id, role FROM episodes "
            "WHERE role IN ('development', 'calibration') ORDER BY episode_id"
        ).fetchall()
        development_ids = tuple(
            row["episode_id"] for row in episode_rows if row["role"] == "development"
        )
        calibration_ids = tuple(
            row["episode_id"] for row in episode_rows if row["role"] == "calibration"
        )
        if not development_ids:
            raise StorageError("operational database has no development episodes")

        development_snapshot_times = _prepare_incident_snapshots(
            operational,
            episode_ids=development_ids,
            simulator_config=simulator_config,
            t0=t0,
            catalog_cutoff=catalog_cutoff,
        )
        development_candidates = _agent_eligible_candidates(
            operational,
            episode_ids=development_ids,
            snapshot_times=development_snapshot_times,
            simulator_config=simulator_config,
            triage_config=triage_config,
            t0=t0,
        )
        selected = development_candidates[:incidents]

        if len(selected) < incidents and calibration_ids:
            calibration_snapshot_times = _prepare_incident_snapshots(
                operational,
                episode_ids=calibration_ids,
                simulator_config=simulator_config,
                t0=t0,
                catalog_cutoff=catalog_cutoff,
            )
            calibration_candidates = _agent_eligible_candidates(
                operational,
                episode_ids=calibration_ids,
                snapshot_times=calibration_snapshot_times,
                simulator_config=simulator_config,
                triage_config=triage_config,
                t0=t0,
            )
            selected.extend(calibration_candidates[: incidents - len(selected)])

        selected_ids = [_selection_entry(candidate) for candidate in selected]
        client_config = agent_provider.client.config
        report: dict[str, Any] = {
            "schema_version": 1,
            "status": "running",
            "provider": provider_name,
            "model_canonical_slug": agent_provider.model_slug,
            "requested_incidents": incidents,
            "cost_cap_usd": client_config.max_total_cost_usd,
            "selected_incidents": selected_ids,
            "calls": [],
            "incident_totals": [],
            "totals": _totals([]),
            "extrapolation": {
                "label": "Estimate for the full design, not a measured benchmark result",
                "design_calls_per_agent_incident": DESIGN_CALLS_PER_INCIDENT,
                "observed_average_cost_per_call_usd": None,
                "estimated_cost_per_agent_incident_usd": None,
                "basis": "Approximately 60 calls per agent incident in MASTER_PLAN.md section 5.",
            },
        }
        if len(selected) < incidents:
            report["status"] = "failed"
            report["error_status"] = "insufficient_agent_eligible_incidents"
            report["available_incidents"] = len(selected)
            _write_report(report_path, report)
            raise MeasurementError(
                f"only {len(selected)} agent-eligible development/calibration incidents "
                f"are available; requested {incidents}"
            )

        # Persist the fixed selection before constructing any LLM request.
        _write_report(report_path, report)
        for selection, candidate in zip(selected_ids, selected, strict=True):
            incident = candidate["incident"]
            if agent_provider.client.spent_cost_usd >= client_config.max_total_cost_usd:
                report["status"] = "budget_stopped"
                report["error_status"] = "cost_limit_exceeded"
                break

            decision = _persist_agent_decision(
                operational,
                incident,
                candidate["checklist_results"],
                as_of_time=selection["as_of_time"],
                provider=agent_provider,
                simulator_config=simulator_config,
                triage_config=triage_config,
            )
            del decision
            new_call_reports = list(agent_provider.last_call_reports)
            for call in new_call_reports:
                report["calls"].append(
                    {
                        **selection,
                        **call,
                    }
                )

            incident_errors = [
                call["error_status"]
                for call in new_call_reports
                if call.get("error_status") is not None
            ]
            incident_status = "failed" if incident_errors else "complete"
            report["incident_totals"].append(
                _incident_totals(selection, new_call_reports, incident_status)
            )
            _refresh_report_totals(report)
            if "cost_limit_exceeded" in incident_errors or (
                agent_provider.client.spent_cost_usd > client_config.max_total_cost_usd
            ):
                report["status"] = "budget_stopped"
                report["error_status"] = "cost_limit_exceeded"
                _write_report(report_path, report)
                break
            if incident_errors:
                report["status"] = "failed"
                report["error_status"] = incident_errors[0]
                _write_report(report_path, report)
                break
            _write_report(report_path, report)

        else:
            report["status"] = "complete"

        _refresh_report_totals(report)
        _write_report(report_path, report)
        return report
