"""P5.0d DEV-FORECAST: pilot-60 agent cost feasibility from development-only reports.

Reads a P5.0b development support report and a development triage measure report,
projects agent cost onto the pilot episode allocation, and writes canonical JSON.
It never calls an LLM and never reads calibration, test or truth data.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from payguard.config import (
    DEFAULT_BENCHMARK_CONFIG,
    ForecastSettings,
    load_benchmark_cost_cap,
    load_benchmark_forecast_settings,
)
from payguard.harness import EPISODES_CONFIG
from simulator.config import load_episode_config


FORECAST_REPORT_TYPE = "p5_0d_pilot_cost_forecast"
SUPPORT_REPORT_TYPE = "p5_0b_development_support"
FORECAST_LABEL = "feasibility estimate, not a benchmark result"
PILOT_PROFILE = "pilot"
DEVELOPMENT_ROLE = "development"


class ForecastError(ValueError):
    """An input report cannot support a development-only pilot cost forecast."""


@dataclass(frozen=True)
class _PilotShape:
    total_episodes: int
    development_episodes: int
    clean_episodes: int
    clean_development_episodes: int


@dataclass(frozen=True)
class _SupportFacts:
    episode_ids: tuple[str, ...]
    agent_incidents: int
    top_episode_id: str
    top_episode_incidents: int


@dataclass(frozen=True)
class _MeasureFacts:
    provider: str
    model_canonical_slug: str
    episode_ids: tuple[str, ...]
    incident_costs: tuple[float, ...]
    call_costs: tuple[float, ...]


def forecast_pilot_cost(
    *,
    support_path: str | Path,
    measure_path: str | Path,
    out_path: str | Path,
    benchmark_config_path: str | Path = DEFAULT_BENCHMARK_CONFIG,
    episodes_config_path: str | Path = EPISODES_CONFIG,
) -> dict[str, Any]:
    """Validate the development inputs, write the canonical forecast JSON and return it."""
    support, support_sha256 = _read_json_report(Path(support_path), "support report")
    measure, measure_sha256 = _read_json_report(Path(measure_path), "measure report")
    pilot = _pilot_shape(Path(episodes_config_path))
    support_facts = _support_facts(support, pilot=pilot)
    facts = _measure_facts(measure, development_ids=frozenset(support_facts.episode_ids))
    settings = load_benchmark_forecast_settings(benchmark_config_path)
    cap_usd = load_benchmark_cost_cap(benchmark_config_path)
    report = _build_report(
        support=support_facts,
        support_sha256=support_sha256,
        measure=facts,
        measure_sha256=measure_sha256,
        settings=settings,
        cap_usd=cap_usd,
        pilot=pilot,
    )
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    output = Path(out_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    return report


def _read_json_report(path: Path, label: str) -> tuple[dict[str, Any], str]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ForecastError(f"cannot read {label} at {path}: {exc}") from exc
    try:
        report = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ForecastError(f"{label} is not UTF-8 JSON: {path}") from exc
    if not isinstance(report, dict):
        raise ForecastError(f"{label} must be a JSON object: {path}")
    return report, hashlib.sha256(raw).hexdigest()


def _pilot_shape(episodes_config_path: Path) -> _PilotShape:
    profile = load_episode_config(episodes_config_path).profile(PILOT_PROFILE)
    return _PilotShape(
        total_episodes=profile.total_episode_count,
        development_episodes=profile.role_episode_counts[DEVELOPMENT_ROLE].value,
        clean_episodes=sum(item.value for item in profile.clean_episode_counts.values()),
        clean_development_episodes=profile.clean_episode_counts[DEVELOPMENT_ROLE].value,
    )


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ForecastError(f"{label} must be a non-negative integer")
    return value


def _finite_nonnegative(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value < 0
    ):
        raise ForecastError(f"{label} must be a finite non-negative number")
    return float(value)


def _support_facts(report: Mapping[str, Any], *, pilot: _PilotShape) -> _SupportFacts:
    if report.get("report_type") != SUPPORT_REPORT_TYPE:
        raise ForecastError(f"support report_type must be {SUPPORT_REPORT_TYPE!r}")
    if report.get("episode_role") != DEVELOPMENT_ROLE:
        raise ForecastError("support episode_role must be 'development'")
    episode_ids = report.get("episode_ids")
    if (
        not isinstance(episode_ids, list)
        or not episode_ids
        or not all(isinstance(item, str) and item for item in episode_ids)
        or len(set(episode_ids)) != len(episode_ids)
    ):
        raise ForecastError("support episode_ids must be a non-empty list of distinct episode IDs")
    if len(episode_ids) != pilot.development_episodes:
        raise ForecastError(
            f"support covers {len(episode_ids)} development episodes; "
            f"the pilot allocation has {pilot.development_episodes}"
        )
    agent_support = report.get("agent_support")
    if not isinstance(agent_support, Mapping):
        raise ForecastError("support agent_support must be an object")
    agent_incidents = _nonnegative_int(
        agent_support.get("incidents_reaching_agent"),
        "support agent_support.incidents_reaching_agent",
    )
    rows = agent_support.get("by_episode")
    if not isinstance(rows, list) or not rows:
        raise ForecastError("support agent_support.by_episode must be a non-empty list")
    per_episode: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("episode_id"), str):
            raise ForecastError("support agent_support.by_episode rows need a string episode_id")
        per_episode[row["episode_id"]] = _nonnegative_int(
            row.get("incidents_reaching_agent"),
            "support agent_support.by_episode.incidents_reaching_agent",
        )
    if set(per_episode) != set(episode_ids):
        raise ForecastError("support by_episode episode IDs must equal episode_ids")
    if sum(per_episode.values()) != agent_incidents:
        raise ForecastError("support by_episode incidents do not sum to incidents_reaching_agent")
    # Largest episode first; ties broken by episode_id so the output is deterministic.
    top_episode_id, top_episode_incidents = min(
        per_episode.items(), key=lambda item: (-item[1], item[0])
    )
    return _SupportFacts(
        episode_ids=tuple(sorted(episode_ids)),
        agent_incidents=agent_incidents,
        top_episode_id=top_episode_id,
        top_episode_incidents=top_episode_incidents,
    )


def _measure_facts(report: Mapping[str, Any], *, development_ids: frozenset[str]) -> _MeasureFacts:
    if report.get("status") != "complete":
        raise ForecastError(f"measure status must be 'complete', got {report.get('status')!r}")
    provider = report.get("provider")
    if not isinstance(provider, str) or not provider or provider == "fake":
        raise ForecastError("measure must come from a real provider; fake costs are not measurements")
    model = report.get("model_canonical_slug")
    if not isinstance(model, str) or not model:
        raise ForecastError("measure model_canonical_slug must be a non-empty string")
    selected = report.get("selected_incidents")
    totals_rows = report.get("incident_totals")
    calls = report.get("calls")
    if not (isinstance(selected, list) and isinstance(totals_rows, list) and isinstance(calls, list)):
        raise ForecastError("measure selected_incidents, incident_totals and calls must be lists")
    if not calls or not totals_rows or len(totals_rows) != len(selected):
        raise ForecastError(
            "measure must contain measured incidents with calls and one totals row per selected incident"
        )
    totals = report.get("totals")
    if not isinstance(totals, Mapping) or totals.get("call_count") != len(calls):
        raise ForecastError("measure totals.call_count must equal the number of recorded calls")
    for row in (*selected, *totals_rows, *calls):
        if not isinstance(row, Mapping):
            raise ForecastError("measure rows must be JSON objects")
        episode_id = row.get("episode_id")
        if not isinstance(episode_id, str) or episode_id not in development_ids:
            raise ForecastError(
                f"measure contains episode {episode_id!r} outside the development support set"
            )
    return _MeasureFacts(
        provider=provider,
        model_canonical_slug=model,
        episode_ids=tuple(sorted({str(row["episode_id"]) for row in totals_rows})),
        incident_costs=tuple(
            _finite_nonnegative(row.get("cost_usd"), "measure incident_totals.cost_usd")
            for row in totals_rows
        ),
        call_costs=tuple(
            _finite_nonnegative(row.get("cost_usd"), "measure calls.cost_usd") for row in calls
        ),
    )


def _usd(value: float) -> float:
    return round(value, 12)


def _branch(
    optimistic: float,
    pessimistic: float | None,
    cap_usd: float,
    **fields: Any,
) -> dict[str, Any]:
    optimistic = _usd(optimistic)
    pessimistic = None if pessimistic is None else _usd(pessimistic)
    return {
        **fields,
        "usd": {"optimistic": optimistic, "pessimistic": pessimistic},
        "within_cap": {
            "optimistic": optimistic <= cap_usd,
            "pessimistic": None if pessimistic is None else pessimistic <= cap_usd,
        },
    }


def _assumptions(
    *,
    support: _SupportFacts,
    pilot: _PilotShape,
    measure: _MeasureFacts,
    factor: float | None,
) -> list[str]:
    dev_count = len(support.episode_ids)
    agent_incidents = support.agent_incidents
    if factor is None:
        pessimistic_rule = (
            "agent_k1 pessimistic = max measured USD per incident x pessimistic_safety_factor; "
            "not reported while the factor is pending"
        )
    else:
        pessimistic_rule = (
            f"agent_k1 pessimistic = max measured USD per incident x {factor} "
            "(pessimistic_safety_factor)"
        )
    return [
        "No LLM call is made; the forecast reads only the development support report and the development measure report.",
        f"Agent incidents per development episode = {agent_incidents} / {dev_count}; projected onto {pilot.total_episodes} pilot episodes.",
        "Held-out clean episodes may raise more or fewer agent incidents than development episodes; calibration and test episodes may differ in the same way, and this projection does not model that difference.",
        f"Clean episodes are {pilot.clean_development_episodes}/{pilot.development_episodes} in development and {pilot.clean_episodes}/{pilot.total_episodes} in the pilot; the projection assumes the same clean share in every role.",
        f"Measured costs come from {len(measure.incident_costs)} incidents and {len(measure.call_costs)} calls on episodes {', '.join(measure.episode_ids)}; this sample is too small to estimate a tail.",
        "Rule branch costs 0 USD: rule-handled incidents make no LLM call.",
        "agent_k1 optimistic = mean measured USD per incident; agent_kK multiplies the K=1 values by K.",
        pessimistic_rule + ".",
        "full_design = projected agent incidents x calls per agent incident x mean measured USD per call; it has no range (pessimistic = optimistic), user decision 05/11/2026.",
        f"Agent incidents are clustered: {support.top_episode_incidents} of {agent_incidents} development agent incidents come from episode {support.top_episode_id}, so the per-episode ratio is rough.",
        "Costs are the provider-reported cost_usd values; prices and token counts are not re-measured.",
        "Feasibility estimate for budget planning only; not a result of the pilot-60 benchmark.",
    ]


def _build_report(
    *,
    support: _SupportFacts,
    support_sha256: str,
    measure: _MeasureFacts,
    measure_sha256: str,
    settings: ForecastSettings,
    cap_usd: float,
    pilot: _PilotShape,
) -> dict[str, Any]:
    agent_incidents = support.agent_incidents
    dev_count = len(support.episode_ids)
    per_episode = agent_incidents / dev_count
    projected = agent_incidents * pilot.total_episodes / dev_count
    mean_incident = sum(measure.incident_costs) / len(measure.incident_costs)
    max_incident = max(measure.incident_costs)
    mean_call = sum(measure.call_costs) / len(measure.call_costs)
    max_call = max(measure.call_costs)
    factor = settings.pessimistic_safety_factor

    branches: dict[str, Any] = {"rule": _branch(0.0, 0.0, cap_usd)}
    for k in settings.agent_k_values:
        pessimistic = None if factor is None else projected * k * max_incident * factor
        branches[f"agent_k{k}"] = _branch(
            projected * k * mean_incident,
            pessimistic,
            cap_usd,
            k=k,
        )
    # Full design has no range (user decision 05/11/2026): pessimistic equals the mean estimate.
    full_design = projected * settings.design_calls_per_agent_incident * mean_call
    branches["full_design"] = _branch(
        full_design,
        full_design,
        cap_usd,
        calls_per_agent_incident=settings.design_calls_per_agent_incident,
    )

    pending = [] if factor is not None else ["pessimistic_safety_factor"]

    return {
        "schema_version": 1,
        "report_type": FORECAST_REPORT_TYPE,
        "label": FORECAST_LABEL,
        "purpose": "pilot_60_cost_feasibility",
        "episode_role": DEVELOPMENT_ROLE,
        "cap_usd": cap_usd,
        "pilot_episode_count": pilot.total_episodes,
        "development_episode_count": dev_count,
        "agent_incidents_development": agent_incidents,
        "agent_incidents_per_development_episode": round(per_episode, 6),
        "projected_agent_incidents_pilot": round(projected, 6),
        "clean_episode_share": {
            "development": round(pilot.clean_development_episodes / pilot.development_episodes, 6),
            "pilot": round(pilot.clean_episodes / pilot.total_episodes, 6),
        },
        "measured": {
            "provider": measure.provider,
            "model_canonical_slug": measure.model_canonical_slug,
            "episode_ids": list(measure.episode_ids),
            "incidents": len(measure.incident_costs),
            "calls": len(measure.call_costs),
            "total_usd": _usd(sum(measure.incident_costs)),
            "mean_usd_per_incident": _usd(mean_incident),
            "max_usd_per_incident": _usd(max_incident),
            "mean_usd_per_call": _usd(mean_call),
            "max_usd_per_call": _usd(max_call),
        },
        "branches": branches,
        "pending_decisions": pending,
        "assumptions": _assumptions(
            support=support,
            pilot=pilot,
            measure=measure,
            factor=factor,
        ),
        "inputs": {
            "support_sha256": support_sha256,
            "measure_sha256": measure_sha256,
        },
    }
