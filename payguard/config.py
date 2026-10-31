"""Validated settings for the benchmark harness."""

from __future__ import annotations

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
