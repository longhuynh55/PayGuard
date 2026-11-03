"""Reference-volume baselines for count-scaled templates (SIM-SCALE).

A count-scaled template injects ``max(floor, round(relative_amplitude * baseline))``
transactions, where the baseline is the monitoring reference median of daily counts for the
injected scope. The baseline follows monitoring's ``reference_median_1d_count``: the median,
over every local business date of the monitoring reference window, of the daily count of
base transactions (zero days included). Channel baselines use the same dates restricted to
one channel. Only base rows available at T0 are read, so no episode, truth, or post-T0 row
can move a baseline.
"""

from __future__ import annotations

import math
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from statistics import median
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

import yaml

from simulator.common import parse_timestamp
from simulator.config import SimulatorConfig

MONITORING_CONFIG_PATH = "configs/monitoring.yaml"
BASELINE_METHOD = "reference_median_1d_count"


class VolumeReferenceError(ValueError):
    """A reference window, baseline, or amplitude cannot anchor a count-scaled template."""


@dataclass(frozen=True)
class ReferenceWindow:
    start_business_date: date
    end_business_date_exclusive: date

    def as_json(self) -> dict[str, str]:
        return {
            "source": f"{MONITORING_CONFIG_PATH} reference",
            "method": BASELINE_METHOD,
            "start_business_date": self.start_business_date.isoformat(),
            "end_business_date_exclusive": self.end_business_date_exclusive.isoformat(),
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> ReferenceWindow:
        if value.get("method") != BASELINE_METHOD:
            raise VolumeReferenceError(f"reference baseline method must be {BASELINE_METHOD}")
        return cls(
            date.fromisoformat(str(value["start_business_date"])),
            date.fromisoformat(str(value["end_business_date_exclusive"])),
        )


@dataclass(frozen=True)
class VolumeBaselines:
    window: ReferenceWindow
    total: float
    by_channel: Mapping[str, float]

    def channel(self, channel: str) -> float:
        return float(self.by_channel.get(channel, 0.0))


def load_reference_window(path: str | Path = MONITORING_CONFIG_PATH) -> ReferenceWindow:
    """Read the monitoring reference window; monitoring.yaml stays the only source."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    reference = raw.get("reference") if isinstance(raw, dict) else None
    if not isinstance(reference, dict):
        raise VolumeReferenceError(f"monitoring config {path} has no reference object")
    start = date.fromisoformat(str(reference["start_business_date"]["value"]))
    end = date.fromisoformat(str(reference["end_business_date_exclusive"]["value"]))
    if start >= end:
        raise VolumeReferenceError("monitoring reference window must be non-empty")
    return ReferenceWindow(start, end)


def reference_volume_baselines(
    operational: sqlite3.Connection,
    config: SimulatorConfig,
    window: ReferenceWindow,
) -> VolumeBaselines:
    """Median daily base count over the reference window, in total and per channel."""
    timezone = ZoneInfo(config.timeline.business_timezone.value)
    t0 = config.timeline.t0_business_date.value
    if window.start_business_date < config.timeline.start_business_date.value:
        raise VolumeReferenceError("reference window starts before the configured base history")
    if window.end_business_date_exclusive > t0:
        raise VolumeReferenceError("reference window must end no later than T0")
    as_of = datetime.combine(t0, time.min, tzinfo=timezone).astimezone(UTC)
    dates = [
        window.start_business_date + timedelta(days=offset)
        for offset in range((window.end_business_date_exclusive - window.start_business_date).days)
    ]
    total_counts: Counter[date] = Counter()
    channel_counts: defaultdict[str, Counter[date]] = defaultdict(Counter)
    for event_at, channel, available_at in operational.execute(
        "SELECT event_at, channel, available_at FROM transactions WHERE episode_id = 'base'"
    ):
        if parse_timestamp(available_at) > as_of:
            continue
        business_date = parse_timestamp(event_at).astimezone(timezone).date()
        if not window.start_business_date <= business_date < window.end_business_date_exclusive:
            continue
        total_counts[business_date] += 1
        channel_counts[str(channel)][business_date] += 1
    return VolumeBaselines(
        window=window,
        total=float(median(total_counts.get(day, 0) for day in dates)),
        by_channel={
            channel: float(median(counts.get(day, 0) for day in dates))
            for channel, counts in sorted(channel_counts.items())
        },
    )


def injected_transaction_count(*, minimum: int, relative_amplitude: float, baseline: float) -> int:
    """Return ``max(minimum, round(relative_amplitude * baseline))`` for a positive baseline."""
    if not math.isfinite(baseline) or baseline <= 0:
        raise VolumeReferenceError("reference median daily count must be a positive finite number")
    if not math.isfinite(relative_amplitude) or relative_amplitude <= 0:
        raise VolumeReferenceError("relative amplitude must be a positive finite number")
    return max(minimum, round(relative_amplitude * baseline))


def calendar_relative_change(
    rows: Sequence[Mapping[str, Any]], *, channel: str, scope_baseline: float
) -> float:
    """Injected relative change on a calendar scope: in-scope rows over the scoped baseline.

    A row outside the channel does not move the scope, so a surge that touches only another
    channel publishes a neutral (zero) expectation.
    """
    if not math.isfinite(scope_baseline) or scope_baseline <= 0:
        raise VolumeReferenceError("calendar scope baseline must be a positive finite number")
    in_scope = sum(1 for row in rows if row["channel"] == channel)
    return in_scope / scope_baseline
