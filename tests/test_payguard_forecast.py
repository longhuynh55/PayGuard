"""Functional tests for `python -m payguard forecast-cost` (P5.0d DEV-FORECAST)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

from payguard.__main__ import main as payguard_main
from payguard.config import BenchmarkConfigError, load_benchmark_forecast_settings
from payguard.forecast import forecast_pilot_cost


BENCHMARK_CONFIG = Path(__file__).resolve().parents[1] / "configs" / "benchmark.yaml"
DEVELOPMENT_EPISODES = tuple(f"ep-{number:04d}" for number in range(1, 13))
# (episode_id, per-call USD) for each measured incident; incident cost = sum of its calls.
HAPPY_INCIDENTS = (("ep-0005", (0.00004, 0.00006)), ("ep-0005", (0.0001, 0.0002)))
# Projection: 36 agent incidents / 12 development episodes x 60 pilot episodes = 180.
PROJECTED_AGENT_INCIDENTS = 180


def _support(
    *,
    episode_ids: Sequence[str] = DEVELOPMENT_EPISODES,
    agent_incidents: int = 36,
    role: str = "development",
    report_type: str = "p5_0b_development_support",
) -> dict[str, Any]:
    # Clustered split: the first development episode holds all but six incidents, the second holds six.
    counts = [max(agent_incidents - 6, 0), min(6, agent_incidents)] + [0] * max(len(episode_ids) - 2, 0)
    by_episode = [
        {"episode_id": episode_id, "incidents_reaching_agent": count, "agent_decision_rows": count}
        for episode_id, count in zip(episode_ids, counts)
    ]
    return {
        "schema_version": 1,
        "report_type": report_type,
        "episode_role": role,
        "episode_ids": list(episode_ids),
        "agent_support": {
            "incidents_reaching_agent": agent_incidents,
            "agent_decision_rows": agent_incidents,
            "by_episode": by_episode,
        },
    }


def _measure(
    incidents: Sequence[tuple[str, Sequence[float]]],
    *,
    status: str = "complete",
    provider: str = "openrouter",
) -> dict[str, Any]:
    selected: list[dict[str, Any]] = []
    totals: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    as_of = "2026-02-05T16:59:59.999999Z"
    for index, (episode_id, call_costs) in enumerate(incidents, start=1):
        incident_id = f"incident-{index:02d}"
        selected.append({"episode_id": episode_id, "incident_id": incident_id, "as_of_time": as_of})
        totals.append(
            {
                "episode_id": episode_id,
                "incident_id": incident_id,
                "as_of_time": as_of,
                "call_count": len(call_costs),
                "cost_usd": sum(call_costs),
                "status": "complete",
            }
        )
        for cost in call_costs:
            calls.append(
                {
                    "episode_id": episode_id,
                    "incident_id": incident_id,
                    "as_of_time": as_of,
                    "request_type": "proposal",
                    "cost_usd": cost,
                    "error_status": None,
                }
            )
    return {
        "schema_version": 1,
        "status": status,
        "provider": provider,
        "model_canonical_slug": "qwen/qwen3.7-flash-20260727",
        "requested_incidents": len(selected),
        "selected_incidents": selected,
        "calls": calls,
        "incident_totals": totals,
        "totals": {"call_count": len(calls), "cost_usd": sum(call["cost_usd"] for call in calls)},
    }


def _write_json(path: Path, report: dict[str, Any]) -> Path:
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _forecast_argv(support: Path, measure: Path, out: Path) -> list[str]:
    return [
        "forecast-cost",
        "--support",
        str(support),
        "--measure",
        str(measure),
        "--out",
        str(out),
    ]


def test_forecast_projects_pilot_60_from_development_reports(tmp_path: Path) -> None:
    support = _write_json(tmp_path / "support.json", _support())
    measure = _write_json(tmp_path / "measure.json", _measure(HAPPY_INCIDENTS))
    out = tmp_path / "forecast.json"
    second = tmp_path / "forecast-again.json"

    assert payguard_main(_forecast_argv(support, measure, out)) == 0
    assert payguard_main(_forecast_argv(support, measure, second)) == 0

    text = out.read_text(encoding="utf-8")
    assert second.read_bytes() == out.read_bytes()
    forecast = json.loads(text)
    assert text == json.dumps(forecast, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    assert forecast["label"] == "feasibility estimate, not a benchmark result"
    assert forecast["pilot_episode_count"] == 60
    assert forecast["projected_agent_incidents_pilot"] == pytest.approx(PROJECTED_AGENT_INCIDENTS)
    assert forecast["inputs"]["support_sha256"] == hashlib.sha256(support.read_bytes()).hexdigest()
    assert forecast["inputs"]["measure_sha256"] == hashlib.sha256(measure.read_bytes()).hexdigest()

    branches = forecast["branches"]
    assert branches["rule"]["usd"] == {"optimistic": 0.0, "pessimistic": 0.0}
    # Mean measured USD per incident is 0.0002; K=3 multiplies it by 3.
    assert branches["agent_k1"]["usd"]["optimistic"] == pytest.approx(180 * 0.0002)
    assert branches["agent_k3"]["usd"]["optimistic"] == pytest.approx(3 * 180 * 0.0002)
    # Mean measured USD per call is 0.0001; the full design uses 60 calls per incident.
    assert branches["full_design"]["usd"]["optimistic"] == pytest.approx(180 * 60 * 0.0001)
    assert all(branch["within_cap"]["optimistic"] is True for branch in branches.values())

    # Safety factor is pinned to 3 by user decision 05/11/2026; the K=1 and K=3 pessimistic
    # bounds use the maximum measured USD per incident (0.0003) times that factor.
    assert load_benchmark_forecast_settings().pessimistic_safety_factor == 3.0
    assert branches["agent_k1"]["usd"]["pessimistic"] == pytest.approx(1 * 180 * 0.0003 * 3.0)
    assert branches["agent_k3"]["usd"]["pessimistic"] == pytest.approx(3 * 180 * 0.0003 * 3.0)
    assert branches["agent_k1"]["within_cap"]["pessimistic"] is True
    assert branches["agent_k3"]["within_cap"]["pessimistic"] is True
    # Full design has no range (user decision 05/11/2026): pessimistic equals the mean estimate.
    assert branches["full_design"]["usd"]["pessimistic"] == branches["full_design"]["usd"]["optimistic"]
    assert branches["full_design"]["within_cap"]["pessimistic"] is True
    assert forecast["pending_decisions"] == []
    assert any(
        item.startswith("Held-out clean episodes may raise more or fewer agent incidents than development episodes")
        for item in forecast["assumptions"]
    )
    assert (
        "Agent incidents are clustered: 30 of 36 development agent incidents come from episode ep-0001, "
        "so the per-episode ratio is rough."
    ) in forecast["assumptions"]


def test_over_cap_branches_are_flagged_and_not_rescaled(tmp_path: Path) -> None:
    support = _write_json(tmp_path / "support.json", _support())
    expensive = _measure((("ep-0005", (0.1, 0.1)), ("ep-0005", (0.2, 0.2))))
    measure = _write_json(tmp_path / "measure.json", expensive)
    out = tmp_path / "forecast.json"

    assert payguard_main(_forecast_argv(support, measure, out)) == 0

    forecast = json.loads(out.read_text(encoding="utf-8"))
    assert forecast["cap_usd"] == 10.0
    # Mean USD per incident is 0.3, so K=1 is 54.0 USD: reported as is, flagged over cap.
    assert forecast["branches"]["agent_k1"]["usd"]["optimistic"] == pytest.approx(180 * 0.3)
    assert forecast["branches"]["agent_k1"]["within_cap"]["optimistic"] is False
    assert forecast["branches"]["agent_k3"]["within_cap"]["optimistic"] is False
    # Mean USD per call is 0.15, so the full design is 1620 USD.
    assert forecast["branches"]["full_design"]["usd"]["optimistic"] == pytest.approx(180 * 60 * 0.15)
    assert forecast["branches"]["full_design"]["within_cap"]["optimistic"] is False


@pytest.mark.parametrize(
    ("support_kwargs", "measure_kwargs"),
    [
        ({"role": "calibration"}, {}),
        ({"report_type": "p5_0c_other_report"}, {}),
        ({"episode_ids": DEVELOPMENT_EPISODES[:1]}, {}),
        ({}, {"status": "budget_stopped"}),
        ({}, {"provider": "fake"}),
        ({}, {"incidents": (("ep-0013", (0.00004, 0.00006)), ("ep-0005", (0.0001, 0.0002)))}),
    ],
)
def test_wrong_role_or_incomplete_input_is_refused_with_exit_2(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    support_kwargs: dict[str, Any],
    measure_kwargs: dict[str, Any],
) -> None:
    measure_kwargs = dict(measure_kwargs)
    incidents = measure_kwargs.pop("incidents", HAPPY_INCIDENTS)
    support = _write_json(tmp_path / "support.json", _support(**support_kwargs))
    measure = _write_json(tmp_path / "measure.json", _measure(incidents, **measure_kwargs))
    out = tmp_path / "forecast.json"

    assert payguard_main(_forecast_argv(support, measure, out)) == 2

    assert not out.exists()
    assert "ForecastError" in capsys.readouterr().err


def test_by_episode_ids_must_match_episode_ids(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    support_report = _support()
    # Same total and same count, but one per-episode row names an episode outside episode_ids.
    support_report["agent_support"]["by_episode"][0]["episode_id"] = "ep-0099"
    support = _write_json(tmp_path / "support.json", support_report)
    measure = _write_json(tmp_path / "measure.json", _measure(HAPPY_INCIDENTS))
    out = tmp_path / "forecast.json"

    assert payguard_main(_forecast_argv(support, measure, out)) == 2

    assert not out.exists()
    assert "ForecastError" in capsys.readouterr().err


def _benchmark_config_with_factor(tmp_path: Path, *, value: object, kind: str) -> Path:
    config = yaml.safe_load(BENCHMARK_CONFIG.read_text(encoding="utf-8"))
    entry = config["benchmark"]["forecast"]["pessimistic_safety_factor"]
    entry["value"] = value
    entry["provenance"]["kind"] = kind
    path = tmp_path / "benchmark.yaml"
    path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def _forecast_with_config(tmp_path: Path, config: Path) -> dict[str, Any]:
    support = _write_json(tmp_path / "support.json", _support())
    measure = _write_json(tmp_path / "measure.json", _measure(HAPPY_INCIDENTS))
    return forecast_pilot_cost(
        support_path=support,
        measure_path=measure,
        out_path=tmp_path / "forecast.json",
        benchmark_config_path=config,
    )


def test_null_factor_with_pending_provenance_is_reported_pending(tmp_path: Path) -> None:
    config = _benchmark_config_with_factor(tmp_path, value=None, kind="pending_decision")

    forecast = _forecast_with_config(tmp_path, config)

    assert forecast["pending_decisions"] == ["pessimistic_safety_factor"]
    assert forecast["branches"]["agent_k1"]["usd"]["pessimistic"] is None
    assert forecast["branches"]["agent_k3"]["within_cap"]["pessimistic"] is None
    # Full design has no range, so its pending state is independent of the factor.
    assert forecast["branches"]["full_design"]["usd"]["pessimistic"] == forecast["branches"]["full_design"]["usd"]["optimistic"]


def test_factor_below_one_is_rejected(tmp_path: Path) -> None:
    config = _benchmark_config_with_factor(tmp_path, value=0.5, kind="assumption")

    with pytest.raises(BenchmarkConfigError):
        _forecast_with_config(tmp_path, config)


def test_value_with_pending_provenance_is_rejected(tmp_path: Path) -> None:
    config = _benchmark_config_with_factor(tmp_path, value=3, kind="pending_decision")

    with pytest.raises(BenchmarkConfigError):
        _forecast_with_config(tmp_path, config)
