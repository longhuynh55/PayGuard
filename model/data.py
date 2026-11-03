"""Build the locked three-way model split from operational as-of snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
import sqlite3
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import yaml

from simulator.asof import read_as_of
from simulator.common import parse_timestamp
from simulator.config import SimulatorConfig
from simulator.labels import LABEL_POLICY_VERSION, LabelSnapshot, SnapshotState, snapshot_labels
from simulator.storage import utc_timestamp


SplitName = Literal["fit", "validation", "calibration"]


@dataclass(frozen=True)
class SplitWindow:
    """A model split window over local business dates."""

    start_inclusive: date
    end_exclusive: date


@dataclass(frozen=True)
class ModelConfig:
    """Validated split boundaries and their documented provenance."""

    business_timezone: str
    provenance: str
    fit: SplitWindow
    validation: SplitWindow
    calibration: SplitWindow

    def windows(self) -> tuple[tuple[SplitName, SplitWindow], ...]:
        return (
            ("fit", self.fit),
            ("validation", self.validation),
            ("calibration", self.calibration),
        )


@dataclass(frozen=True)
class ModelExample:
    """One observed-label training example, without simulator-private fields."""

    txn_id: str
    episode_id: str
    event_at: str
    label_state: SnapshotState
    target: int


@dataclass(frozen=True)
class SplitAudit:
    """Counts for candidates, exclusions, and retained binary-label support."""

    name: SplitName
    window: SplitWindow
    candidate_count: int
    age_eligible_count: int
    under_horizon_excluded_count: int
    pending_excluded_count: int
    sample_count: int
    positive_count: int
    negative_count: int


@dataclass(frozen=True)
class SplitAuditReport:
    """Reproducible audit summary for the three datasets."""

    episode_id: str
    as_of_time: str
    horizon_days: int
    label_policy_version: str
    splits: tuple[SplitAudit, ...]

    def for_split(self, name: SplitName) -> SplitAudit:
        return next(item for item in self.splits if item.name == name)

    def format(self) -> str:
        lines = [
            f"episode={self.episode_id} as_of_time={self.as_of_time} "
            f"H={self.horizon_days}d label_policy_version={self.label_policy_version}"
        ]
        for item in self.splits:
            lines.append(
                f"{item.name} [{item.window.start_inclusive}, {item.window.end_exclusive}): "
                f"n={item.sample_count} positive={item.positive_count} "
                f"negative={item.negative_count} candidates={item.candidate_count} "
                f"age_eligible={item.age_eligible_count} "
                f"under_H_excluded={item.under_horizon_excluded_count} "
                f"pending_excluded={item.pending_excluded_count}"
            )
        return "\n".join(lines)


@dataclass(frozen=True)
class ModelSplits:
    """Rows and audit report produced from one operational episode snapshot."""

    episode_id: str
    as_of_time: str
    label_policy_version: str
    fit: tuple[ModelExample, ...]
    validation: tuple[ModelExample, ...]
    calibration: tuple[ModelExample, ...]
    audit: SplitAuditReport


class InsufficientSplitSupportError(ValueError):
    """Raised when a split lacks either class required by the MVP pipeline."""

    def __init__(self, report: SplitAuditReport, missing_support: tuple[str, ...]) -> None:
        self.report = report
        self.missing_support = missing_support
        details = "; ".join(missing_support)
        super().__init__(
            "insufficient split support (minimum 1 example for each of classes 0 and 1): "
            f"{details}\n{report.format()}"
        )


def _parse_date(value: object, *, field: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"model config {field} must be an ISO date string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"model config {field} must be an ISO date string") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"model config {field} must use YYYY-MM-DD format")
    return parsed


def load_model_config(path: str | Path = "configs/model.yaml") -> ModelConfig:
    """Load and validate split boundaries and their provenance from YAML."""
    config_path = Path(path)
    try:
        with config_path.open("r", encoding="utf-8") as stream:
            raw = yaml.safe_load(stream)
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"could not read model config {config_path}: {exc}") from exc

    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("model config must be a mapping with schema_version: 1")
    if raw.get("boundary_convention") != "start_inclusive_end_exclusive":
        raise ValueError("model split boundary convention must be start-inclusive/end-exclusive")
    timezone_name = raw.get("business_timezone")
    if not isinstance(timezone_name, str) or not timezone_name:
        raise ValueError("model config business_timezone must be a non-empty IANA timezone")
    try:
        ZoneInfo(timezone_name)
    except (ValueError, KeyError) as exc:
        raise ValueError(f"unknown model business timezone: {timezone_name!r}") from exc

    provenance = raw.get("provenance")
    if (
        not isinstance(provenance, dict)
        or provenance.get("kind") != "contract"
        or not isinstance(provenance.get("reference"), str)
        or not provenance["reference"].strip()
    ):
        raise ValueError("model split config requires contract provenance")

    raw_splits = raw.get("splits")
    names = ("fit", "validation", "calibration")
    if not isinstance(raw_splits, dict) or set(raw_splits) != set(names):
        raise ValueError("model config splits must define fit, validation, and calibration")
    windows: dict[str, SplitWindow] = {}
    for name in names:
        entry = raw_splits[name]
        if not isinstance(entry, dict) or set(entry) != {"start_inclusive", "end_exclusive"}:
            raise ValueError(f"model config {name} must define start_inclusive and end_exclusive")
        window = SplitWindow(
            start_inclusive=_parse_date(entry["start_inclusive"], field=f"{name}.start_inclusive"),
            end_exclusive=_parse_date(entry["end_exclusive"], field=f"{name}.end_exclusive"),
        )
        if window.start_inclusive >= window.end_exclusive:
            raise ValueError(f"model config {name} must have a non-empty date range")
        windows[name] = window

    if windows["fit"].end_exclusive != windows["validation"].start_inclusive:
        raise ValueError("fit and validation split boundaries must be adjacent")
    if windows["validation"].end_exclusive != windows["calibration"].start_inclusive:
        raise ValueError("validation and calibration split boundaries must be adjacent")

    return ModelConfig(
        business_timezone=timezone_name,
        provenance=provenance["reference"],
        fit=windows["fit"],
        validation=windows["validation"],
        calibration=windows["calibration"],
    )


def target_for_label_state(state: SnapshotState | str) -> int | None:
    """Map operational snapshot states to supervised targets; omit pending rows."""
    if state == "confirmed_fraud":
        return 1
    if state in {"confirmed_legit", "matured_negative"}:
        return 0
    if state == "pending":
        return None
    raise ValueError(f"unsupported label snapshot state: {state!r}")


def _snapshot_target(snapshot: LabelSnapshot) -> int | None:
    target = target_for_label_state(snapshot.state)
    expected_label = {
        "confirmed_fraud": "fraud",
        "confirmed_legit": "legit",
        "matured_negative": None,
        "pending": None,
    }[snapshot.state]
    if snapshot.label != expected_label:
        raise ValueError(
            f"snapshot label does not match state {snapshot.state!r} for {snapshot.txn_id!r}"
        )
    return target


def _snapshot_time(simulator_config: SimulatorConfig, timezone_name: str) -> datetime:
    local_t0 = datetime.combine(
        simulator_config.timeline.t0_business_date.value,
        time.min,
        tzinfo=ZoneInfo(timezone_name),
    )
    return local_t0.astimezone(UTC)


def _business_date(event_at: str, timezone_name: str) -> date:
    return parse_timestamp(event_at).astimezone(ZoneInfo(timezone_name)).date()


def build_splits(
    operational: sqlite3.Connection,
    simulator_config: SimulatorConfig,
    *,
    episode_id: str,
    model_config: ModelConfig | None = None,
) -> ModelSplits:
    """Read operational transactions and label states as of T0 and audit the split.

    The caller supplies only the operational connection. Transaction and label rows
    are obtained through ``read_as_of`` inside the simulator APIs; this function has
    no private-store argument and does not inspect generator truth.
    """
    split_config = model_config or load_model_config()
    if split_config.business_timezone != simulator_config.timeline.business_timezone.value:
        raise ValueError("model and simulator business timezones must match")

    t0 = _snapshot_time(simulator_config, split_config.business_timezone)
    as_of_time = utc_timestamp(t0)
    horizon_days = simulator_config.label_reporting.maturity_horizon_days.value
    horizon = timedelta(days=horizon_days)
    transactions = read_as_of(
        operational,
        "transactions",
        episode_id=episode_id,
        t0=as_of_time,
        as_of_time=as_of_time,
    )
    snapshots = snapshot_labels(
        operational,
        simulator_config,
        episode_id=episode_id,
        as_of_time=as_of_time,
    )
    snapshots_by_id = {snapshot.txn_id: snapshot for snapshot in snapshots}
    if len(snapshots_by_id) != len(snapshots):
        raise ValueError("label snapshot contains duplicate transaction IDs")

    windows = dict(split_config.windows())
    rows: dict[SplitName, list[ModelExample]] = {
        "fit": [],
        "validation": [],
        "calibration": [],
    }
    counts: dict[SplitName, dict[str, int]] = {
        name: {
            "candidate_count": 0,
            "age_eligible_count": 0,
            "under_horizon_excluded_count": 0,
            "pending_excluded_count": 0,
            "positive_count": 0,
            "negative_count": 0,
        }
        for name in windows
    }

    for transaction in transactions:
        txn_id = transaction["txn_id"]
        snapshot = snapshots_by_id.get(txn_id)
        if snapshot is None:
            raise ValueError(f"label snapshot is missing transaction {txn_id!r}")
        if snapshot.event_at != transaction["event_at"]:
            raise ValueError(f"label snapshot event time differs for transaction {txn_id!r}")

        event_date = _business_date(transaction["event_at"], split_config.business_timezone)
        split_name = next(
            (
                name
                for name, window in windows.items()
                if window.start_inclusive <= event_date < window.end_exclusive
            ),
            None,
        )
        if split_name is None:
            continue

        split_counts = counts[split_name]
        split_counts["candidate_count"] += 1
        event_time = parse_timestamp(transaction["event_at"])
        age = t0 - event_time
        if age < timedelta(0):
            raise ValueError(f"transaction {txn_id!r} occurs after the T0 label snapshot")
        if age < horizon:
            split_counts["under_horizon_excluded_count"] += 1
        else:
            split_counts["age_eligible_count"] += 1

        target = _snapshot_target(snapshot)
        if target is None:
            split_counts["pending_excluded_count"] += 1
            continue
        if age < horizon:
            continue

        rows[split_name].append(
            ModelExample(
                txn_id=txn_id,
                episode_id=transaction["episode_id"],
                event_at=transaction["event_at"],
                label_state=snapshot.state,
                target=target,
            )
        )
        split_counts["positive_count" if target == 1 else "negative_count"] += 1

    for split_rows in rows.values():
        split_rows.sort(key=lambda row: (row.event_at, row.txn_id))

    audit_splits = tuple(
        SplitAudit(
            name=name,
            window=windows[name],
            sample_count=len(rows[name]),
            **counts[name],
        )
        for name in ("fit", "validation", "calibration")
    )
    report = SplitAuditReport(
        episode_id=episode_id,
        as_of_time=as_of_time,
        horizon_days=horizon_days,
        label_policy_version=LABEL_POLICY_VERSION,
        splits=audit_splits,
    )
    missing_support_items: list[str] = []
    for item in audit_splits:
        if item.positive_count < 1:
            missing_support_items.append(
                f"{item.name}: missing positive class support "
                f"(positive={item.positive_count}, negative={item.negative_count})"
            )
        if item.negative_count < 1:
            missing_support_items.append(
                f"{item.name}: missing negative class support "
                f"(positive={item.positive_count}, negative={item.negative_count})"
            )
    missing_support = tuple(missing_support_items)
    if missing_support:
        raise InsufficientSplitSupportError(report, missing_support)

    return ModelSplits(
        episode_id=episode_id,
        as_of_time=as_of_time,
        label_policy_version=LABEL_POLICY_VERSION,
        fit=tuple(rows["fit"]),
        validation=tuple(rows["validation"]),
        calibration=tuple(rows["calibration"]),
        audit=report,
    )
