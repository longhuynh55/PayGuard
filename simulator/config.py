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
EpisodeTemplateName = Literal[
    "clean",
    "data_pipeline",
    "seasonal",
    "fraud_surge",
    "fraud_calendar",
    "two_cause_fraud_seasonal",
    "new_fraud",
    "legit_business_spike",
    "new_merchant_sparse_history",
    "silent_concept_drift",
]


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
    account_role_weights: ConfigValue[dict[str, NonNegativeFloat]]
    familiar_account_payee_count: ConfigValue[PositiveInt]
    household_supplier_merchant_count: ConfigValue[PositiveInt]
    internal_mule_outbound_window_hours: ConfigValue[PositiveInt]
    legitimate_device_change_probability: ConfigValue[Probability]
    legitimate_new_accounts_per_base_history: ConfigValue[NonNegativeInt]
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

    @field_validator("account_role_weights")
    @classmethod
    def account_role_weights_must_cover_all_roles(
        cls, value: ConfigValue[dict[str, NonNegativeFloat]]
    ) -> ConfigValue[dict[str, NonNegativeFloat]]:
        weights = value.value
        if set(weights) != {"individual", "household_business", "internal_mule"}:
            raise ValueError(
                "world account_role_weights must define individual, household_business, and internal_mule"
            )
        if any(not math.isfinite(weight) or weight <= 0 for weight in weights.values()):
            raise ValueError("world account_role_weights must be finite and positive")
        if not math.isclose(sum(weights.values()), 1.0, rel_tol=0.0, abs_tol=1e-9):
            raise ValueError("world account_role_weights must sum to 1")
        return value


class TransactionConfig(StrictModel):
    minimum_amount: ConfigValue[PositiveInt]
    maximum_amount: ConfigValue[PositiveInt]
    channel: ConfigValue[str]
    channel_weights: ConfigValue[dict[str, NonNegativeFloat]]
    account_transfer_channel: ConfigValue[str]
    account_transfer_rate_per_individual_local_day: ConfigValue[PositiveFloat]
    refund_probability: ConfigValue[Probability]
    reversal_probability: ConfigValue[Probability]
    adjustment_minimum_delay_minutes: ConfigValue[PositiveInt]
    adjustment_maximum_delay_hours: ConfigValue[PositiveInt]
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

    @field_validator("channel_weights")
    @classmethod
    def channel_weights_must_cover_all_channels(
        cls, value: ConfigValue[dict[str, NonNegativeFloat]]
    ) -> ConfigValue[dict[str, NonNegativeFloat]]:
        weights = value.value
        if set(weights) != {"qr", "wallet", "card"}:
            raise ValueError("transactions channel_weights must define qr, wallet, and card")
        if any(not math.isfinite(weight) or weight <= 0 for weight in weights.values()):
            raise ValueError("transactions channel_weights must be finite and positive")
        if not math.isclose(sum(weights.values()), 1.0, rel_tol=0.0, abs_tol=1e-9):
            raise ValueError("transactions channel_weights must sum to 1")
        return value

    @field_validator("account_transfer_channel")
    @classmethod
    def account_transfer_channel_is_wallet(
        cls, value: ConfigValue[str]
    ) -> ConfigValue[str]:
        if value.value != "wallet":
            raise ValueError("transactions.account_transfer_channel is fixed to 'wallet'")
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
        if self.refund_probability.value + self.reversal_probability.value > 1:
            raise ValueError("transactions refund and reversal probabilities must sum to at most 1")
        if (
            self.adjustment_maximum_delay_hours.value * 60
            < self.adjustment_minimum_delay_minutes.value
        ):
            raise ValueError("transactions adjustment delay range must be ordered")
        lifecycle_delay_seconds = (
            self.authorization_delay_seconds.value
            + self.settlement_delay_minutes.value * 60
        )
        if lifecycle_delay_seconds >= 24 * 60 * 60:
            raise ValueError("transactions authorization and settlement delays must total less than one day")
        return self


class CardTestingConfig(StrictModel):
    channel: ConfigValue[str]
    campaign_interval_days: ConfigValue[PositiveInt]
    start_hour_local: ConfigValue[Annotated[int, Field(ge=0, le=23)]]
    benign_control_delay_minutes: ConfigValue[PositiveInt]
    approval_probability: ConfigValue[Probability]
    followup_approval_probability: ConfigValue[Probability]
    attempts_per_card: ConfigValue[PositiveInt]
    window_minutes: ConfigValue[PositiveInt]
    minimum_attempt_amount: ConfigValue[PositiveInt]
    maximum_attempt_amount: ConfigValue[PositiveInt]
    minimum_followup_amount: ConfigValue[PositiveInt]
    maximum_followup_amount: ConfigValue[PositiveInt]
    followup_delay_seconds: ConfigValue[PositiveInt]

    @model_validator(mode="after")
    def card_testing_parameters_are_ordered(self) -> CardTestingConfig:
        if self.channel.value != "card":
            raise ValueError("fraud.card_testing channel is fixed to 'card' for P1.4")
        if self.minimum_attempt_amount.unit != MVP_MONEY_UNIT:
            raise ValueError(f"fraud.card_testing amount unit must be exactly '{MVP_MONEY_UNIT}'")
        if self.maximum_attempt_amount.unit != MVP_MONEY_UNIT:
            raise ValueError(f"fraud.card_testing amount unit must be exactly '{MVP_MONEY_UNIT}'")
        if self.minimum_followup_amount.unit != MVP_MONEY_UNIT:
            raise ValueError(f"fraud.card_testing amount unit must be exactly '{MVP_MONEY_UNIT}'")
        if self.maximum_followup_amount.unit != MVP_MONEY_UNIT:
            raise ValueError(f"fraud.card_testing amount unit must be exactly '{MVP_MONEY_UNIT}'")
        if self.minimum_attempt_amount.value > self.maximum_attempt_amount.value:
            raise ValueError("fraud.card_testing attempt amount range must be ordered")
        if self.minimum_followup_amount.value > self.maximum_followup_amount.value:
            raise ValueError("fraud.card_testing follow-up amount range must be ordered")
        if self.minimum_followup_amount.value <= self.maximum_attempt_amount.value:
            raise ValueError("fraud.card_testing follow-up amount must exceed every small attempt")
        if self.window_minutes.value * 60 < self.attempts_per_card.value:
            raise ValueError("fraud.card_testing window must allow distinct second-level attempt times")
        if self.start_hour_local.unit != "local wall-clock hour in Asia/Ho_Chi_Minh":
            raise ValueError("fraud.card_testing start_hour_local must use Vietnam local wall-clock hours")
        if (
            self.start_hour_local.value * 60 + self.benign_control_delay_minutes.value
            >= 24 * 60
        ):
            raise ValueError("fraud.card_testing benign control must start on the same business date")
        return self


class ATOConfig(StrictModel):
    campaign_interval_days: ConfigValue[PositiveInt]
    start_offset_days: ConfigValue[NonNegativeInt]
    start_hour_local: ConfigValue[Annotated[int, Field(ge=0, le=23)]]
    transaction_count: ConfigValue[PositiveInt]
    transaction_spacing_days: ConfigValue[PositiveInt]
    device_signature_probability: ConfigValue[Probability]
    approval_probability: ConfigValue[Probability]
    channel: ConfigValue[Literal["qr", "wallet", "card"]]

    @field_validator("start_hour_local")
    @classmethod
    def ato_hour_uses_vietnam_local_time(cls, value: ConfigValue[int]) -> ConfigValue[int]:
        if value.unit != "local wall-clock hour in Asia/Ho_Chi_Minh":
            raise ValueError("fraud.ato.start_hour_local must use Vietnam local wall-clock hours")
        return value


class CollusionConfig(StrictModel):
    campaign_interval_days: ConfigValue[PositiveInt]
    start_hour_local: ConfigValue[Annotated[int, Field(ge=0, le=23)]]
    sender_count: ConfigValue[PositiveInt]
    transfer_spacing_minutes: ConfigValue[PositiveInt]
    cashout_delay_minutes: ConfigValue[PositiveInt]

    @field_validator("start_hour_local")
    @classmethod
    def collusion_hour_uses_vietnam_local_time(cls, value: ConfigValue[int]) -> ConfigValue[int]:
        if value.unit != "local wall-clock hour in Asia/Ho_Chi_Minh":
            raise ValueError("fraud.collusion.start_hour_local must use Vietnam local wall-clock hours")
        return value

    @model_validator(mode="after")
    def collusion_group_has_multiple_senders(self) -> CollusionConfig:
        if self.sender_count.value < 2:
            raise ValueError("fraud.collusion.sender_count must be at least two")
        return self


class SocialEngineeringConfig(StrictModel):
    campaign_interval_days: ConfigValue[PositiveInt]
    start_offset_days: ConfigValue[NonNegativeInt]
    new_account_warmup_days: ConfigValue[NonNegativeInt]
    start_hour_local: ConfigValue[Annotated[int, Field(ge=0, le=23)]]
    fan_in_count: ConfigValue[PositiveInt]
    older_sender_minimum_count: ConfigValue[NonNegativeInt]
    incoming_spacing_days: ConfigValue[PositiveInt]
    cashout_delay_minutes: ConfigValue[PositiveInt]
    business_control_minimum_amount: ConfigValue[PositiveInt]
    business_control_maximum_amount: ConfigValue[PositiveInt]

    @field_validator("start_hour_local")
    @classmethod
    def social_hour_uses_vietnam_local_time(cls, value: ConfigValue[int]) -> ConfigValue[int]:
        if value.unit != "local wall-clock hour in Asia/Ho_Chi_Minh":
            raise ValueError("fraud.social_engineering.start_hour_local must use Vietnam local wall-clock hours")
        return value

    @field_validator("business_control_minimum_amount", "business_control_maximum_amount")
    @classmethod
    def business_control_amounts_use_vnd(cls, value: ConfigValue[int]) -> ConfigValue[int]:
        if value.unit != MVP_MONEY_UNIT:
            raise ValueError("fraud.social_engineering control amount unit must be integer VND")
        return value

    @model_validator(mode="after")
    def social_parameters_are_ordered(self) -> SocialEngineeringConfig:
        if self.start_offset_days.value <= self.new_account_warmup_days.value:
            raise ValueError(
                "fraud.social_engineering start_offset_days must leave the configured "
                "new-account warmup inside the episode"
            )
        if self.fan_in_count.value < 2:
            raise ValueError("fraud.social_engineering.fan_in_count must be at least two")
        if self.older_sender_minimum_count.value > self.fan_in_count.value:
            raise ValueError("fraud.social_engineering older-sender count cannot exceed fan-in count")
        if self.business_control_minimum_amount.value > self.business_control_maximum_amount.value:
            raise ValueError("fraud.social_engineering business control amount range must be ordered")
        return self


class FraudTimingJitterConfig(StrictModel):
    maximum_minute_offset: ConfigValue[Annotated[int, Field(ge=1, le=59)]]
    maximum_second_offset: ConfigValue[Annotated[int, Field(ge=1, le=59)]]
    cashout_delay_jitter_minutes: ConfigValue[NonNegativeInt]
    mule_creation_jitter_minutes: ConfigValue[PositiveInt]
    card_testing_start_hour_jitter: ConfigValue[Annotated[int, Field(ge=0, le=12)]]
    card_testing_spacing_jitter_seconds: ConfigValue[NonNegativeInt]
    card_testing_followup_jitter_seconds: ConfigValue[NonNegativeInt]


class FraudConfig(StrictModel):
    card_testing: CardTestingConfig
    ato: ATOConfig
    collusion: CollusionConfig
    social_engineering: SocialEngineeringConfig
    timing_jitter: FraudTimingJitterConfig
    near_threshold_minimum_amount: ConfigValue[PositiveInt]
    near_threshold_maximum_amount: ConfigValue[PositiveInt]

    @field_validator("near_threshold_minimum_amount", "near_threshold_maximum_amount")
    @classmethod
    def fraud_amount_band_uses_vnd(cls, value: ConfigValue[int]) -> ConfigValue[int]:
        if value.unit != MVP_MONEY_UNIT:
            raise ValueError("fraud near-threshold amount unit must be integer VND")
        return value

    @model_validator(mode="after")
    def fraud_amount_band_is_ordered(self) -> FraudConfig:
        if self.near_threshold_minimum_amount.value > self.near_threshold_maximum_amount.value:
            raise ValueError("fraud near-threshold amount range must be ordered")
        return self


class LabelReportingConfig(StrictModel):
    maturity_horizon_days: ConfigValue[PositiveInt]
    minimum_delay_days: ConfigValue[NonNegativeInt]
    maximum_delay_days: ConfigValue[NonNegativeInt]
    delay_distribution: ConfigValue[Literal["uniform_integer"]]
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


class EpisodeRuntimeConfig(StrictModel):
    duration_days: ConfigValue[PositiveInt]

    @model_validator(mode="after")
    def duration_must_match_mvp_contract(self) -> EpisodeRuntimeConfig:
        if self.duration_days.value != 30:
            raise ValueError("episode duration_days is fixed at 30 by the MVP contract")
        return self


class SimulatorConfig(StrictModel):
    schema_version: Literal[1]
    seed: ConfigValue[NonNegativeInt]
    episode: EpisodeRuntimeConfig
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
        card_testing = self.fraud.card_testing
        if card_testing.minimum_attempt_amount.value < self.transactions.minimum_amount.value:
            raise ValueError("fraud.card_testing attempt amounts must be within the transaction amount range")
        if card_testing.maximum_followup_amount.value > self.transactions.maximum_amount.value:
            raise ValueError("fraud.card_testing follow-up amounts must be within the transaction amount range")
        if self.fraud.near_threshold_minimum_amount.value < self.transactions.minimum_amount.value:
            raise ValueError("fraud near-threshold amounts must be within the transaction amount range")
        if self.fraud.near_threshold_maximum_amount.value > self.transactions.maximum_amount.value:
            raise ValueError("fraud near-threshold amounts must be within the transaction amount range")
        if card_testing.campaign_interval_days.value > (
            self.timeline.t0_business_date.value - self.timeline.start_business_date.value
        ).days:
            raise ValueError("fraud.card_testing campaign interval must allow a pre-T0 campaign")
        card_jitter = self.fraud.timing_jitter
        spacing_seconds = (
            card_testing.window_minutes.value * 60 // card_testing.attempts_per_card.value
        )
        spacing_jitter_seconds = card_jitter.card_testing_spacing_jitter_seconds.value
        if (
            spacing_seconds <= spacing_jitter_seconds
            or (card_testing.attempts_per_card.value - 1)
            * (spacing_seconds + spacing_jitter_seconds)
            >= card_testing.window_minutes.value * 60
        ):
            raise ValueError("fraud.card_testing spacing jitter must keep attempts inside its configured window")
        if (
            card_testing.followup_delay_seconds.value
            <= card_jitter.card_testing_followup_jitter_seconds.value
        ):
            raise ValueError("fraud.card_testing follow-up jitter must keep a positive post-window delay")
        start_hour_jitter = card_jitter.card_testing_start_hour_jitter.value
        if (
            card_testing.start_hour_local.value < start_hour_jitter
            or card_testing.start_hour_local.value
            + math.ceil(card_testing.benign_control_delay_minutes.value / 60)
            + start_hour_jitter
            >= 24
        ):
            raise ValueError("fraud.card_testing start-hour jitter must keep both groups on the same business date")
        attempt_window_seconds = card_testing.window_minutes.value * 60
        lifecycle_delay_seconds = (
            self.transactions.authorization_delay_seconds.value
            + self.transactions.settlement_delay_minutes.value * 60
        )
        fraud_end_seconds = (
            (card_testing.start_hour_local.value + start_hour_jitter) * 60 * 60
            + attempt_window_seconds
            + card_testing.followup_delay_seconds.value
            + card_jitter.card_testing_followup_jitter_seconds.value
            + lifecycle_delay_seconds
        )
        control_end_seconds = (
            (card_testing.start_hour_local.value + start_hour_jitter) * 60 * 60
            + card_testing.benign_control_delay_minutes.value * 60
            + attempt_window_seconds
            + lifecycle_delay_seconds
        )
        if max(fraud_end_seconds, control_end_seconds) >= 24 * 60 * 60:
            raise ValueError("fraud.card_testing lifecycle must finish before the next business date")
        social = self.fraud.social_engineering
        timing_jitter = self.fraud.timing_jitter
        if social.business_control_maximum_amount.value > self.transactions.maximum_amount.value:
            raise ValueError("fraud.social_engineering control amounts must be within the transaction amount range")
        if (
            self.fraud.near_threshold_maximum_amount.value
            * social.fan_in_count.value
            > (2**63 - 1)
        ):
            raise ValueError("fraud social-engineering transfer totals exceed SQLite integer range")
        if (
            (social.fan_in_count.value - 1) * social.incoming_spacing_days.value >= 7
            or social.start_offset_days.value
            + (social.fan_in_count.value - 1) * social.incoming_spacing_days.value
            >= self.episode.duration_days.value
        ):
            raise ValueError("fraud.social_engineering transfers must fit the observed 7-day and episode windows")
        if social.cashout_delay_minutes.value * 60 <= lifecycle_delay_seconds:
            raise ValueError("fraud.social_engineering cash-out must follow incoming settlement")
        if social.cashout_delay_minutes.value >= self.world.internal_mule_outbound_window_hours.value * 60:
            raise ValueError("fraud social-engineering cash-out must fit the mule outbound window")
        if (
            social.cashout_delay_minutes.value
            + timing_jitter.cashout_delay_jitter_minutes.value
            >= self.world.internal_mule_outbound_window_hours.value * 60
        ):
            raise ValueError(
                "fraud social-engineering cash-out after timing jitter must fit the mule outbound window"
            )
        minimum_cashout_delay = (
            min(
                self.fraud.collusion.cashout_delay_minutes.value,
                social.cashout_delay_minutes.value,
            )
            - timing_jitter.cashout_delay_jitter_minutes.value
        )
        if minimum_cashout_delay * 60 <= lifecycle_delay_seconds:
            raise ValueError(
                "fraud cash-out delay after timing jitter must follow incoming settlement"
            )
        if timing_jitter.mule_creation_jitter_minutes.value >= (
            social.start_offset_days.value - social.new_account_warmup_days.value
        ) * 24 * 60:
            raise ValueError(
                "fraud timing jitter must keep the social-engineering mule's history window inside the episode"
            )
        ato = self.fraud.ato
        if (
            ato.start_offset_days.value
            + (ato.transaction_count.value - 1) * ato.transaction_spacing_days.value
            >= self.episode.duration_days.value
        ):
            raise ValueError("fraud.ato transaction schedule must fit the episode duration")
        if self.fraud.collusion.campaign_interval_days.value > (
            self.timeline.drift_start_business_date.value
            - self.timeline.start_business_date.value
        ).days:
            raise ValueError("fraud.collusion campaign interval must allow a pre-drift campaign")
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


class EpisodeAssignment(StrictModel):
    episode_id: str = Field(pattern=r"^ep-[0-9]{4}$")
    role: EpisodeRole
    template: EpisodeTemplateName


class DataPipelineTemplateConfig(StrictModel):
    mode: ConfigValue[Literal["multiply_100", "null"]]


class SeasonalTemplateConfig(StrictModel):
    date_offset_days: ConfigValue[PositiveInt]
    event_name: ConfigValue[str]
    additional_transaction_count: ConfigValue[PositiveInt]
    expected_relative_change: ConfigValue[PositiveFloat]


class DateOffsetTemplateConfig(StrictModel):
    date_offset_days: ConfigValue[PositiveInt]


class SparseMerchantTemplateConfig(StrictModel):
    date_offset_days: ConfigValue[PositiveInt]
    transaction_count: ConfigValue[Annotated[int, Field(ge=2, le=5)]]


class SilentConceptDriftTemplateConfig(StrictModel):
    date_offset_days: ConfigValue[PositiveInt]
    transaction_count: ConfigValue[PositiveInt]


class LegitimateNewAccountControlsConfig(StrictModel):
    accounts_per_episode: ConfigValue[PositiveInt]
    additional_accounts_without_social_engineering: ConfigValue[PositiveInt]
    creation_offset_days: ConfigValue[NonNegativeInt]
    creation_in_window_offset_days: ConfigValue[PositiveInt]
    creation_in_window_jitter_days: ConfigValue[NonNegativeInt]
    creation_time_jitter_seconds: ConfigValue[PositiveInt]
    early_first_transaction_offset_days: ConfigValue[PositiveInt]
    early_transactions_per_account: ConfigValue[PositiveInt]
    first_transaction_offset_days: ConfigValue[PositiveInt]
    transactions_per_account: ConfigValue[PositiveInt]
    timing_match_transactions_per_account: ConfigValue[PositiveInt]
    minimum_account_age_days: ConfigValue[PositiveInt]

    @model_validator(mode="after")
    def account_history_is_observable(self) -> LegitimateNewAccountControlsConfig:
        if self.accounts_per_episode.value < 2:
            raise ValueError("N7 requires an early and an in-window legitimate control account")
        if self.additional_accounts_without_social_engineering.value != 1:
            raise ValueError(
                "N7 episode customer-count parity requires one additional ordinary account "
                "when no social-engineering mule is generated"
            )
        if self.creation_in_window_offset_days.value <= self.creation_offset_days.value:
            raise ValueError("N7 in-window account creation must follow the early control account")
        latest_creation_offset = (
            self.creation_in_window_offset_days.value
            + self.creation_in_window_jitter_days.value
        )
        if self.early_first_transaction_offset_days.value - self.creation_offset_days.value < (
            self.minimum_account_age_days.value
        ):
            raise ValueError(
                "early legitimate new-account transactions must start after the full minimum "
                "history window"
            )
        if self.first_transaction_offset_days.value - latest_creation_offset < (
            self.minimum_account_age_days.value
        ):
            raise ValueError(
                "legitimate new-account controls must be available for their full minimum "
                "history window before the first transaction"
            )
        if self.creation_time_jitter_seconds.value > 86399:
            raise ValueError("N7 creation_time_jitter_seconds must be at most 86399")
        if self.early_first_transaction_offset_days.value >= self.first_transaction_offset_days.value:
            raise ValueError(
                "N7 early account activity must precede the later account transaction schedule"
            )
        return self


class EpisodeTemplateConfig(StrictModel):
    data_pipeline: DataPipelineTemplateConfig
    seasonal: SeasonalTemplateConfig
    fraud_surge: DateOffsetTemplateConfig
    fraud_calendar: DateOffsetTemplateConfig
    new_merchant_sparse_history: SparseMerchantTemplateConfig
    silent_concept_drift: SilentConceptDriftTemplateConfig
    legitimate_new_account_controls: LegitimateNewAccountControlsConfig


class EpisodeProfile(StrictModel):
    role_episode_counts: dict[EpisodeRole, ConfigValue[PositiveInt]]
    clean_episode_counts: dict[EpisodeRole, ConfigValue[NonNegativeInt]]
    multi_cause_episode_count: ConfigValue[NonNegativeInt]
    assignments: list[EpisodeAssignment] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_allocations(self) -> EpisodeProfile:
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
        if self.multi_cause_episode_count.value > total - clean_total:
            raise ValueError("multi-cause episodes must be included among injected episodes")
        episode_ids = [assignment.episode_id for assignment in self.assignments]
        if len(episode_ids) != len(set(episode_ids)):
            raise ValueError("episode assignment IDs must be unique within a profile")
        if self.assignments and len(self.assignments) != total:
            raise ValueError("episode assignment count must match role_episode_counts")
        if self.assignments:
            assigned_roles = {role: 0 for role in ("development", "calibration", "test")}
            for assignment in self.assignments:
                assigned_roles[assignment.role] += 1
            if assigned_roles != episode_counts:
                raise ValueError("episode assignments must match role_episode_counts")
        return self

    @property
    def total_episode_count(self) -> int:
        return sum(value.value for value in self.role_episode_counts.values())


class EpisodeConfig(StrictModel):
    schema_version: Literal[1]
    seed: ConfigValue[NonNegativeInt]
    protocol: EpisodeProtocol
    templates: EpisodeTemplateConfig
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
        if not pilot.assignments:
            raise ValueError("pilot profile must declare its locked episode assignments")
        expected_template_role_counts = {
            "clean": {"development": 4, "calibration": 8, "test": 8},
            "two_cause_fraud_seasonal": {"development": 2, "calibration": 4, "test": 4},
            "fraud_calendar": {"development": 1, "calibration": 2, "test": 2},
            "seasonal": {"development": 1, "calibration": 2, "test": 2},
            "data_pipeline": {"development": 1, "calibration": 2, "test": 2},
            "fraud_surge": {"development": 1, "calibration": 2, "test": 2},
            "new_fraud": {"development": 1, "calibration": 1, "test": 1},
            "legit_business_spike": {"development": 1, "calibration": 1, "test": 1},
            "new_merchant_sparse_history": {"development": 0, "calibration": 1, "test": 1},
            "silent_concept_drift": {"development": 0, "calibration": 1, "test": 1},
        }
        actual_template_role_counts = {
            template: {role: 0 for role in ("development", "calibration", "test")}
            for template in expected_template_role_counts
        }
        for assignment in pilot.assignments:
            if assignment.template not in actual_template_role_counts:
                raise ValueError(
                    f"pilot template {assignment.template!r} is outside the locked allocation"
                )
            actual_template_role_counts[assignment.template][assignment.role] += 1
        if actual_template_role_counts != expected_template_role_counts:
            raise ValueError(
                "pilot template-by-role assignments must match the locked docs/data.md section 4 table"
            )
        if pilot.multi_cause_episode_count.value != 10:
            raise ValueError("pilot must contain ten locked fraud-plus-seasonal episodes")
        if pilot.total_episode_count != 60:
            raise ValueError("pilot must remain the locked 60-episode profile")

        if not 3 <= smoke.total_episode_count <= 6:
            raise ValueError("round-one smoke profile must contain 3 through 6 episodes")
        if not smoke.assignments:
            raise ValueError("round-one smoke profile must declare locked episode assignments")
        smoke_role_counts = {role: 0 for role in ("development", "calibration", "test")}
        smoke_clean_counts = {role: 0 for role in smoke_role_counts}
        for assignment in smoke.assignments:
            smoke_role_counts[assignment.role] += 1
            if assignment.template == "clean":
                smoke_clean_counts[assignment.role] += 1
        if smoke_role_counts != {
            role: value.value for role, value in smoke.role_episode_counts.items()
        }:
            raise ValueError("smoke assignments must match role_episode_counts")
        if smoke_clean_counts != {
            role: value.value for role, value in smoke.clean_episode_counts.items()
        }:
            raise ValueError("smoke clean assignments must match clean_episode_counts")
        if smoke.multi_cause_episode_count.value != 0:
            raise ValueError("round-one smoke episodes support at most one incident each")
        if self.templates.seasonal.date_offset_days.value >= self.protocol.duration_days.value:
            raise ValueError("seasonal date_offset_days must fall inside an episode")
        for name in ("fraud_surge", "fraud_calendar"):
            if getattr(self.templates, name).date_offset_days.value >= self.protocol.duration_days.value:
                raise ValueError(f"templates.{name}.date_offset_days must fall inside an episode")
        if self.templates.new_merchant_sparse_history.date_offset_days.value >= self.protocol.duration_days.value:
            raise ValueError("new merchant date_offset_days must fall inside an episode")
        if self.templates.silent_concept_drift.date_offset_days.value >= self.protocol.duration_days.value:
            raise ValueError("silent concept-drift date_offset_days must fall inside an episode")
        if (
            max(
                self.templates.legitimate_new_account_controls.early_first_transaction_offset_days.value
                + self.templates.legitimate_new_account_controls.early_transactions_per_account.value,
                self.templates.legitimate_new_account_controls.first_transaction_offset_days.value
                + self.templates.legitimate_new_account_controls.transactions_per_account.value,
            )
            > self.protocol.duration_days.value
        ):
            raise ValueError("legitimate new-account transactions must fit inside an episode")
        templates = {assignment.template for assignment in smoke.assignments}
        if not {"clean", "data_pipeline", "seasonal"} <= templates:
            raise ValueError("round-one smoke must include clean, data_pipeline, and seasonal episodes")
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


def validate_episode_duration_match(
    simulator_config: SimulatorConfig,
    episode_config: EpisodeConfig,
) -> None:
    """Ensure the public runtime horizon matches the injector protocol horizon."""
    public_duration = simulator_config.episode.duration_days.value
    protocol_duration = episode_config.protocol.duration_days.value
    if public_duration != protocol_duration:
        raise ConfigValidationError(
            "simulator episode.duration_days "
            f"({public_duration}) must match episodes protocol.duration_days "
            f"({protocol_duration})"
        )
    controls = episode_config.templates.legitimate_new_account_controls
    if (
        simulator_config.fraud.social_engineering.start_offset_days.value
        + controls.timing_match_transactions_per_account.value
        > protocol_duration
    ):
        raise ConfigValidationError(
            "N7 timing-match account transactions must fit inside the episode duration"
        )


def load_simulator_config(path: str | Path = "configs/simulator.yaml") -> SimulatorConfig:
    """Read and validate simulator configuration from YAML."""
    return validate_simulator_config(_read_yaml(path))


def load_episode_config(path: str | Path = "configs/episodes.yaml") -> EpisodeConfig:
    """Read and validate episode configuration from YAML."""
    return validate_episode_config(_read_yaml(path))
