"""Load and validate simulator and episode YAML configuration."""

from __future__ import annotations

import math
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any, Generic, Literal, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


T = TypeVar("T")
Probability = Annotated[float, Field(ge=0.0, le=1.0)]
PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]
NonNegativeFloat = Annotated[float, Field(ge=0.0)]
PositiveFloat = Annotated[float, Field(gt=0.0)]
MVP_MONEY_UNIT = "integer VND"
ProfileName = Literal["smoke", "pilot"]
EpisodeRole = Literal["development", "calibration", "test"]


class ConfigValidationError(ValueError):
    """Raised when YAML cannot be parsed or violates the P1.1 config contract."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Provenance(StrictModel):
    kind: Literal["contract", "user_decision", "source", "assumption"]
    reference: str = Field(min_length=1)

    @field_validator("reference")
    @classmethod
    def reference_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("provenance reference must not be blank")
        return value


class ConfigValue(StrictModel, Generic[T]):
    value: T
    unit: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    provenance: Provenance

    @field_validator("unit", "rationale")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class TimelineConfig(StrictModel):
    storage_timezone: ConfigValue[str]
    business_timezone: ConfigValue[str]
    start_business_date: ConfigValue[date]
    end_business_date_exclusive: ConfigValue[date]
    t0_business_date: ConfigValue[date]
    drift_start_business_date: ConfigValue[date]
    monitoring_job_time_local: ConfigValue[str]
    salary_days_of_month: ConfigValue[list[int]]
    promotion_month_day: ConfigValue[str]

    @field_validator("storage_timezone", "business_timezone")
    @classmethod
    def timezone_must_exist(cls, value: ConfigValue[str]) -> ConfigValue[str]:
        try:
            ZoneInfo(value.value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA timezone: {value.value}") from exc
        return value

    @field_validator("monitoring_job_time_local")
    @classmethod
    def local_job_time_must_be_hhmm(cls, value: ConfigValue[str]) -> ConfigValue[str]:
        try:
            datetime.strptime(value.value, "%H:%M")
        except ValueError as exc:
            raise ValueError("must be a valid local time in HH:MM format") from exc
        return value

    @field_validator("salary_days_of_month")
    @classmethod
    def salary_days_must_be_unique_calendar_days(
        cls, value: ConfigValue[list[int]]
    ) -> ConfigValue[list[int]]:
        days = value.value
        if not days or any(day < 1 or day > 31 for day in days):
            raise ValueError("must contain calendar days from 1 through 31")
        if len(days) != len(set(days)):
            raise ValueError("must not contain duplicate calendar days")
        return value

    @field_validator("promotion_month_day")
    @classmethod
    def promotion_day_must_be_mmdd(cls, value: ConfigValue[str]) -> ConfigValue[str]:
        try:
            datetime.strptime(value.value, "%m-%d")
        except ValueError as exc:
            raise ValueError("must be a valid month-day in MM-DD format") from exc
        return value

    @model_validator(mode="after")
    def timeline_must_be_ordered(self) -> TimelineConfig:
        if self.storage_timezone.value != "UTC":
            raise ValueError("timeline storage_timezone is fixed at UTC by the MVP contract")
        if self.business_timezone.value != "Asia/Ho_Chi_Minh":
            raise ValueError("timeline business_timezone is fixed at Asia/Ho_Chi_Minh by the MVP contract")
        start = self.start_business_date.value
        end = self.end_business_date_exclusive.value
        t0 = self.t0_business_date.value
        drift = self.drift_start_business_date.value
        if start >= end:
            raise ValueError("timeline start_business_date must precede end_business_date_exclusive")
        if not start <= t0 < end:
            raise ValueError("timeline t0_business_date must be within the configured date range")
        if not t0 < drift < end:
            raise ValueError("timeline drift_start_business_date must follow T0 and precede the end date")
        return self


class WorldConfig(StrictModel):
    expected_transactions_per_customer_local_day: ConfigValue[PositiveFloat]
    minimum_customer_count: ConfigValue[PositiveInt]
    minimum_merchant_count: ConfigValue[PositiveInt]
    customer_segments: ConfigValue[list[str]]
    customer_provinces: ConfigValue[list[str]]
    customer_age_bands: ConfigValue[list[str]]
    merchant_mcc_codes: ConfigValue[list[str]]
    merchant_provinces: ConfigValue[list[str]]

    @field_validator(
        "customer_segments",
        "customer_provinces",
        "customer_age_bands",
        "merchant_mcc_codes",
        "merchant_provinces",
    )
    @classmethod
    def profile_categories_must_be_unique_nonempty_values(
        cls, value: ConfigValue[list[str]]
    ) -> ConfigValue[list[str]]:
        values = value.value
        if not values or any(not item.strip() for item in values):
            raise ValueError("profile categories must contain non-empty values")
        if len(values) != len(set(values)):
            raise ValueError("profile categories must not contain duplicates")
        return value


class TransactionConfig(StrictModel):
    minimum_amount: ConfigValue[PositiveInt]
    maximum_amount: ConfigValue[PositiveInt]
    channel: ConfigValue[str]
    authorization_response_code: ConfigValue[str]
    authorization_delay_seconds: ConfigValue[PositiveInt]
    hourly_transaction_weights: ConfigValue[list[NonNegativeFloat]]
    settlement_delay_minutes: ConfigValue[PositiveInt]

    @field_validator("channel")
    @classmethod
    def channel_must_be_nonempty(cls, value: ConfigValue[str]) -> ConfigValue[str]:
        if not value.value.strip():
            raise ValueError("transactions channel must not be blank")
        return value

    @field_validator("authorization_response_code")
    @classmethod
    def authorization_response_code_must_be_nonempty(
        cls, value: ConfigValue[str]
    ) -> ConfigValue[str]:
        if not value.value.strip():
            raise ValueError("transactions authorization_response_code must not be blank")
        return value

    @field_validator("hourly_transaction_weights")
    @classmethod
    def hourly_weights_must_cover_local_day(
        cls, value: ConfigValue[list[NonNegativeFloat]]
    ) -> ConfigValue[list[NonNegativeFloat]]:
        if len(value.value) != 24:
            raise ValueError("transactions hourly weights must contain one value for each local hour")
        if any(not math.isfinite(weight) for weight in value.value):
            raise ValueError("transactions hourly weights must be finite numbers")
        if not any(weight > 0 for weight in value.value):
            raise ValueError("transactions hourly weights must contain a positive value")
        return value

    @model_validator(mode="after")
    def amount_range_must_be_ordered(self) -> TransactionConfig:
        if (
            self.minimum_amount.unit != MVP_MONEY_UNIT
            or self.maximum_amount.unit != MVP_MONEY_UNIT
        ):
            raise ValueError(f"transactions amount unit must be exactly '{MVP_MONEY_UNIT}'")
        if self.minimum_amount.value >= self.maximum_amount.value:
            raise ValueError("transactions minimum_amount must be less than maximum_amount")
        if self.minimum_amount.unit != self.maximum_amount.unit:
            raise ValueError("transactions minimum_amount and maximum_amount must use the same unit")
        lifecycle_delay_seconds = (
            self.authorization_delay_seconds.value
            + self.settlement_delay_minutes.value * 60
        )
        if lifecycle_delay_seconds >= 24 * 60 * 60:
            raise ValueError("transactions authorization and settlement delays must total less than one day")
        return self


class CardTestingConfig(StrictModel):
    approval_probability: ConfigValue[Probability]
    attempts_per_card: ConfigValue[PositiveInt]
    window_minutes: ConfigValue[PositiveInt]


class FraudConfig(StrictModel):
    card_testing: CardTestingConfig


class LabelReportingConfig(StrictModel):
    maturity_horizon_days: ConfigValue[PositiveInt]
    minimum_delay_days: ConfigValue[NonNegativeInt]
    maximum_delay_days: ConfigValue[NonNegativeInt]
    probability_reported: ConfigValue[Probability]

    @field_validator("maturity_horizon_days")
    @classmethod
    def maturity_horizon_is_fixed_at_90_days(
        cls, value: ConfigValue[PositiveInt]
    ) -> ConfigValue[PositiveInt]:
        if value.value != 90:
            raise ValueError("label_reporting maturity_horizon_days is fixed at 90 by the MVP contract")
        return value

    @model_validator(mode="after")
    def delay_range_must_be_ordered(self) -> LabelReportingConfig:
        if self.minimum_delay_days.value > self.maximum_delay_days.value:
            raise ValueError("label_reporting minimum_delay_days must not exceed maximum_delay_days")
        return self


class ScaleProfile(StrictModel):
    customer_count: ConfigValue[PositiveInt]
    merchant_count: ConfigValue[PositiveInt]


class SimulatorConfig(StrictModel):
    schema_version: Literal[1]
    seed: ConfigValue[NonNegativeInt]
    timeline: TimelineConfig
    world: WorldConfig
    transactions: TransactionConfig
    fraud: FraudConfig
    label_reporting: LabelReportingConfig
    profiles: dict[ProfileName, ScaleProfile]

    @model_validator(mode="after")
    def require_smoke_and_pilot_profiles(self) -> SimulatorConfig:
        if set(self.profiles) != {"smoke", "pilot"}:
            raise ValueError("profiles must define exactly 'smoke' and 'pilot'")
        smoke = self.profiles["smoke"]
        pilot = self.profiles["pilot"]
        if smoke.customer_count.value >= pilot.customer_count.value:
            raise ValueError("smoke customer_count must be lower than pilot customer_count")
        if smoke.merchant_count.value >= pilot.merchant_count.value:
            raise ValueError("smoke merchant_count must be lower than pilot merchant_count")
        for profile_name, profile in self.profiles.items():
            if profile.customer_count.value < self.world.minimum_customer_count.value:
                raise ValueError(f"{profile_name} customer_count is below the configured minimum")
            if profile.merchant_count.value < self.world.minimum_merchant_count.value:
                raise ValueError(f"{profile_name} merchant_count is below the configured minimum")
        return self

    def scale_for(self, profile: ProfileName) -> ScaleProfile:
        """Return counts for a profile; behavioral and timeline settings stay shared."""
        return self.profiles[profile]


class EpisodeProtocol(StrictModel):
    duration_days: ConfigValue[PositiveInt]
    minimum_incidents: ConfigValue[NonNegativeInt]
    maximum_incidents: ConfigValue[NonNegativeInt]

    @model_validator(mode="after")
    def incident_range_must_be_ordered(self) -> EpisodeProtocol:
        if self.minimum_incidents.value > self.maximum_incidents.value:
            raise ValueError("protocol minimum_incidents must not exceed maximum_incidents")
        if self.duration_days.value != 30:
            raise ValueError("protocol duration_days is fixed at 30 by the MVP contract")
        if self.minimum_incidents.value != 0 or self.maximum_incidents.value != 2:
            raise ValueError("protocol incident range is fixed at 0 through 2 by the MVP contract")
        return self


class EpisodeProfile(StrictModel):
    role_episode_counts: dict[EpisodeRole, ConfigValue[PositiveInt]]
    clean_episode_counts: dict[EpisodeRole, ConfigValue[NonNegativeInt]]
    multi_cause_episode_count: ConfigValue[NonNegativeInt]

    @model_validator(mode="after")
    def enforce_pilot_proportions(self) -> EpisodeProfile:
        roles = {"development", "calibration", "test"}
        if set(self.role_episode_counts) != roles or set(self.clean_episode_counts) != roles:
            raise ValueError("each episode profile must define development, calibration, and test roles")

        episode_counts = {role: value.value for role, value in self.role_episode_counts.items()}
        clean_counts = {role: value.value for role, value in self.clean_episode_counts.items()}
        total = sum(episode_counts.values())
        clean_total = sum(clean_counts.values())
        if total == 0:
            raise ValueError("episode profile must contain at least one episode")
        if any(clean_counts[role] > episode_counts[role] for role in roles):
            raise ValueError("clean episode count cannot exceed the role's episode count")
        if episode_counts["development"] * 5 != total:
            raise ValueError("development episodes must be one fifth of each profile")
        if episode_counts["calibration"] * 5 != total * 2:
            raise ValueError("calibration episodes must be two fifths of each profile")
        if episode_counts["test"] * 5 != total * 2:
            raise ValueError("test episodes must be two fifths of each profile")
        if clean_total * 3 != total:
            raise ValueError("clean episodes must remain one third of each profile")
        if any(clean_counts[role] * 3 != episode_counts[role] for role in roles):
            raise ValueError("each role must retain the pilot clean share")
        if self.multi_cause_episode_count.value * 15 != total:
            raise ValueError("multi-cause episodes must retain the pilot proportion of one per 15")
        if self.multi_cause_episode_count.value > total - clean_total:
            raise ValueError("multi-cause episodes must be included among injected episodes")
        return self

    @property
    def total_episode_count(self) -> int:
        return sum(value.value for value in self.role_episode_counts.values())


class EpisodeConfig(StrictModel):
    schema_version: Literal[1]
    seed: ConfigValue[NonNegativeInt]
    protocol: EpisodeProtocol
    profiles: dict[ProfileName, EpisodeProfile]

    @model_validator(mode="after")
    def require_smoke_and_pilot_profiles(self) -> EpisodeConfig:
        if set(self.profiles) != {"smoke", "pilot"}:
            raise ValueError("profiles must define exactly 'smoke' and 'pilot'")
        smoke = self.profiles["smoke"]
        pilot = self.profiles["pilot"]
        expected_pilot_counts = {"development": 12, "calibration": 24, "test": 24}
        expected_pilot_clean_counts = {"development": 4, "calibration": 8, "test": 8}
        if {role: value.value for role, value in pilot.role_episode_counts.items()} != expected_pilot_counts:
            raise ValueError("pilot role counts must match the locked 12/24/24 episode manifest")
        if {role: value.value for role, value in pilot.clean_episode_counts.items()} != expected_pilot_clean_counts:
            raise ValueError("pilot clean role counts must match the locked 4/8/8 allocation")
        if pilot.multi_cause_episode_count.value != 4:
            raise ValueError("pilot must contain the four locked multi-cause episodes")
        if smoke.total_episode_count >= pilot.total_episode_count:
            raise ValueError("smoke episode count must be lower than pilot episode count")
        if smoke.total_episode_count * 4 != pilot.total_episode_count:
            raise ValueError("smoke episode profile must be one quarter of pilot scale")
        return self

    def profile(self, profile: ProfileName) -> EpisodeProfile:
        """Return a scale-only profile under the shared episode protocol."""
        return self.profiles[profile]


def _read_yaml(path: str | Path) -> Any:
    config_path = Path(path)
    try:
        with config_path.open("r", encoding="utf-8") as stream:
            return yaml.safe_load(stream)
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigValidationError(f"Could not read YAML config {config_path}: {exc}") from exc


def _validate(model: type[BaseModel], data: Any, label: str) -> Any:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or label}: {error['msg']}"
            for error in exc.errors()
        )
        raise ConfigValidationError(f"Invalid {label} config: {details}") from exc


def validate_simulator_config(data: Any) -> SimulatorConfig:
    """Validate a parsed simulator config mapping."""
    return _validate(SimulatorConfig, data, "simulator")


def validate_episode_config(data: Any) -> EpisodeConfig:
    """Validate a parsed episode config mapping."""
    return _validate(EpisodeConfig, data, "episode")


def load_simulator_config(path: str | Path = "configs/simulator.yaml") -> SimulatorConfig:
    """Read and validate simulator configuration from YAML."""
    return validate_simulator_config(_read_yaml(path))


def load_episode_config(path: str | Path = "configs/episodes.yaml") -> EpisodeConfig:
    """Read and validate episode configuration from YAML."""
    return validate_episode_config(_read_yaml(path))
