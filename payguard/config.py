"""Validated settings for the benchmark harness."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BENCHMARK_CONFIG = ROOT / "configs" / "benchmark.yaml"


class BenchmarkConfigError(ValueError):
    """Raised when a locked benchmark setting is missing or invalid."""


def load_benchmark_cost_cap(path: str | Path = DEFAULT_BENCHMARK_CONFIG) -> float:
    """Load the benchmark cap and enforce its documented source."""
    config_path = Path(path)
    try:
        config: Any = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        entry = config["benchmark"]["cost_cap_usd"]
        value = entry["value"]
        provenance = entry["provenance"]
    except (OSError, KeyError, TypeError, yaml.YAMLError) as exc:
        raise BenchmarkConfigError(
            f"benchmark cost cap is missing or invalid in {config_path}"
        ) from exc
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value <= 0
    ):
        raise BenchmarkConfigError("benchmark cost_cap_usd.value must be finite and positive")
    if (
        not isinstance(provenance, dict)
        or provenance.get("kind") != "locked_contract"
        or "MASTER_PLAN.md" not in str(provenance.get("reference", ""))
        or "section 5" not in str(provenance.get("reference", "")).lower()
    ):
        raise BenchmarkConfigError(
            "benchmark cost cap provenance must reference MASTER_PLAN.md section 5"
        )
    return float(value)


@dataclass(frozen=True)
class ForecastSettings:
    """Inputs of the P5.0d cost forecast; the safety factor is None while pending."""

    design_calls_per_agent_incident: int
    agent_k_values: tuple[int, ...]
    pessimistic_safety_factor: float | None


def _forecast_entry(forecast: Any, name: str, config_path: Path) -> tuple[Any, str]:
    entry = forecast[name]
    provenance = entry["provenance"]
    kind = provenance.get("kind")
    reference = str(provenance.get("reference", "")).strip()
    if not kind or not reference:
        raise BenchmarkConfigError(f"forecast {name} needs provenance kind and reference in {config_path}")
    return entry["value"], str(kind)


def load_benchmark_forecast_settings(
    path: str | Path = DEFAULT_BENCHMARK_CONFIG,
) -> ForecastSettings:
    """Load the forecast settings; a null safety factor must be marked pending_decision."""
    config_path = Path(path)
    try:
        config: Any = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        forecast = config["benchmark"]["forecast"]
        calls, _ = _forecast_entry(forecast, "design_calls_per_agent_incident", config_path)
        k_values, _ = _forecast_entry(forecast, "agent_k_values", config_path)
        factor, factor_kind = _forecast_entry(forecast, "pessimistic_safety_factor", config_path)
    except (OSError, KeyError, TypeError, AttributeError, yaml.YAMLError) as exc:
        raise BenchmarkConfigError(
            f"benchmark forecast settings are missing or invalid in {config_path}"
        ) from exc
    if isinstance(calls, bool) or not isinstance(calls, int) or calls <= 0:
        raise BenchmarkConfigError("forecast design_calls_per_agent_incident must be a positive integer")
    if (
        not isinstance(k_values, list)
        or not k_values
        or len(set(k_values)) != len(k_values)
        or any(isinstance(k, bool) or not isinstance(k, int) or k <= 0 for k in k_values)
    ):
        raise BenchmarkConfigError("forecast agent_k_values must be distinct positive integers")
    if factor is None:
        if factor_kind != "pending_decision":
            raise BenchmarkConfigError("a null pessimistic_safety_factor must have kind pending_decision")
        return ForecastSettings(calls, tuple(sorted(k_values)), None)
    if factor_kind == "pending_decision":
        raise BenchmarkConfigError("pessimistic_safety_factor with a value must not stay pending_decision")
    if (
        isinstance(factor, bool)
        or not isinstance(factor, (int, float))
        or not math.isfinite(float(factor))
        or factor < 1
    ):
        raise BenchmarkConfigError("pessimistic_safety_factor must be a finite number >= 1")
    return ForecastSettings(calls, tuple(sorted(k_values)), float(factor))
