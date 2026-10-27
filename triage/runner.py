"""Daily operational monitoring-to-triage loop for a generated run."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, time, timedelta
from pathlib import Path
import sqlite3
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from monitoring.incidents import OperationalDecisionSource, compute_incidents
from monitoring.store import persist_monitoring_rows
from simulator.asof import read_as_of
from simulator.config import load_simulator_config
from simulator.storage import StorageError, utc_timestamp
from triage.agent import AgentProposalProvider, UnconfiguredAgent
from triage.decisions import persist_decisions
from triage.gate import GateConfig, evaluate_gate
from triage.runbooks import check_unit_or_null_change, load_triage_config


@contextmanager
def _open_operational(path: Path) -> Iterator[sqlite3.Connection]:
    if not path.is_file():
        raise FileNotFoundError(f"operational database does not exist: {path}")
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise StorageError("SQLite foreign key enforcement could not be enabled")
        attached = [
            row[1]
            for row in connection.execute("PRAGMA database_list")
            if row[1] not in {"main", "temp"}
        ]
        if attached:
            raise StorageError(f"operational connection has attached databases: {attached}")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _latest_snapshots(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        current = latest.get(row["incident_id"])
        if current is None or (row["event_at"], row["available_at"]) > (
            current["event_at"],
            current["available_at"],
        ):
            latest[row["incident_id"]] = row
    return latest


def _proposal_for_incident(
    operational: sqlite3.Connection,
    incident: Mapping[str, Any],
    *,
    as_of_time: str,
    simulator_config,
    triage_config: Mapping[str, Any],
    agent_provider: AgentProposalProvider,
) -> tuple[str, dict[str, Any]]:
    checklists = check_unit_or_null_change(
        operational,
        incident,
        as_of_time=as_of_time,
        simulator_config=simulator_config,
        triage_config=triage_config,
    )
    unit_result = checklists.get("unit_or_null_change", {})
    if unit_result.get("status") == "pass" and unit_result.get("scope_match") is True:
        return "rule", {
            "cause": "data_pipeline",
            "runbook": "unit_or_null_change",
            "action": "data_ticket",
            "checklist_results": checklists,
        }

    proposal = dict(
        agent_provider.propose(
            incident,
            as_of_time=as_of_time,
            checklist_results=checklists,
        )
    )
    # Evidence is code-owned. A proposal may choose cause/action, but cannot
    # replace the operational checklist or supply evidence_status.
    proposal["checklist_results"] = checklists
    proposal.pop("evidence_status", None)
    return "agent", proposal


def run_triage(
    run_dir: str | Path,
    *,
    simulator_config_path: str | Path = "configs/simulator.yaml",
    triage_config_path: str | Path = "configs/triage.yaml",
    agent_provider: AgentProposalProvider | None = None,
) -> dict[str, int]:
    """Run deterministic daily monitoring, rule gate, and decision persistence.

    The job reads and writes only ``operational/observations.sqlite``. It uses
    persisted P3 metrics and alerts as the public calendar of monitoring days,
    then reduces incidents one Vietnam business day at a time before gating.
    """

    root = Path(run_dir).expanduser().resolve()
    operational_path = root / "operational" / "observations.sqlite"
    simulator_config = load_simulator_config(simulator_config_path)
    triage_config = load_triage_config(triage_config_path)
    gate_config = GateConfig.from_mapping(triage_config)
    provider = agent_provider or UnconfiguredAgent()
    timezone = ZoneInfo(simulator_config.timeline.business_timezone.value)
    t0 = utc_timestamp(
        datetime.combine(
            simulator_config.timeline.t0_business_date.value,
            time.min,
            tzinfo=timezone,
        )
    )
    catalog_cutoff = utc_timestamp(
        datetime.combine(
            simulator_config.timeline.end_business_date_exclusive.value - timedelta(days=1),
            time.max,
            tzinfo=timezone,
        )
    )

    metric_rows_seen = 0
    alert_rows_seen = 0
    incidents_inserted = 0
    decisions_inserted = 0
    with _open_operational(operational_path) as operational:
        episode_ids = tuple(
            row[0]
            for row in operational.execute(
                "SELECT episode_id FROM episodes WHERE episode_id <> 'base' ORDER BY episode_id"
            )
        )
        if not episode_ids:
            raise StorageError("operational database has no registered non-base episodes")

        metric_catalog: list[dict[str, Any]] = []
        for episode_id in episode_ids:
            metric_catalog.extend(
                read_as_of(
                    operational,
                    "daily_metrics",
                    episode_id=episode_id,
                    t0=t0,
                    as_of_time=catalog_cutoff,
                )
            )
        if not metric_catalog:
            raise StorageError(
                "P3 daily_metrics are missing; run monitoring metrics before triage"
            )
        snapshot_times = tuple(sorted({row["as_of_time"] for row in metric_catalog}))
        if any(row["available_at"] > catalog_cutoff for row in metric_catalog):
            raise StorageError("daily_metrics catalog contains a row unavailable in the run timeline")
        metric_rows_seen = len(metric_catalog)

        for as_of_time in snapshot_times:
            for episode_id in episode_ids:
                alert_snapshot = read_as_of(
                    operational,
                    "alerts",
                    episode_id=episode_id,
                    t0=t0,
                    as_of_time=as_of_time,
                )
                alert_rows_seen += len(
                    [row for row in alert_snapshot if row["as_of_time"] == as_of_time]
                )
                decision_source = OperationalDecisionSource(
                    operational=operational,
                    t0=t0,
                    as_of_time=as_of_time,
                )
                incident_result = compute_incidents(
                    alert_snapshot,
                    episode_id=episode_id,
                    decision_source=decision_source,
                )
                current_incident_rows = [
                    row
                    for row in incident_result.incidents
                    if row["available_at"] == as_of_time
                ]
                incidents_inserted += persist_monitoring_rows(
                    operational, "incidents", current_incident_rows
                )

                incident_snapshot = read_as_of(
                    operational,
                    "incidents",
                    episode_id=episode_id,
                    t0=t0,
                    as_of_time=as_of_time,
                )
                current_open = [
                    row
                    for row in _latest_snapshots(incident_snapshot).values()
                    if row["status"] in {"open", "recheck"}
                    and row["available_at"] == as_of_time
                ]
                for incident in current_open:
                    source, proposal = _proposal_for_incident(
                        operational,
                        incident,
                        as_of_time=as_of_time,
                        simulator_config=simulator_config,
                        triage_config=triage_config,
                        agent_provider=provider,
                    )
                    decision = evaluate_gate(
                        incident,
                        proposal,
                        source=source,
                        expected_episode_id=episode_id,
                        as_of_time=as_of_time,
                        config=gate_config,
                    )
                    decisions_inserted += persist_decisions(operational, (decision,))

        operational.commit()
        return {
            "episode_count": len(episode_ids),
            "daily_metric_rows_seen": metric_rows_seen,
            "alert_rows_seen": alert_rows_seen,
            "incident_snapshots_inserted": incidents_inserted,
            "decision_rows_inserted": decisions_inserted,
            "decision_rows_total": operational.execute(
                "SELECT COUNT(*) FROM decisions"
            ).fetchone()[0],
        }
