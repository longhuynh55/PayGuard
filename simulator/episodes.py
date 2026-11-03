"""Generate locked smoke and pilot episodes with private manifests."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from simulator.config import EpisodeAssignment, EpisodeConfig, ProfileName, SimulatorConfig
from simulator.fraud import (
    FraudSummary,
    generate_card_testing,
    generate_other_fraud_mechanisms,
    social_engineering_customer_schedule,
)
from simulator.identifiers import customer_id as generate_customer_id
from simulator.identifiers import transaction_id
from simulator.incidents import (
    IncidentSummary,
    card_testing_burst_size,
    inject_data_pipeline,
    inject_fraud_surge,
    inject_legit_business_spike,
    inject_new_fraud,
    inject_seasonal,
    inject_silent_concept_drift,
    inject_sparse_new_merchant,
    inject_two_cause_fraud_seasonal,
)
from simulator.labels import LabelSummary, generate_label_events
from simulator.randomness import derive_seed, keyed_rng
from simulator.reproducibility import canonical_episode_fingerprint
from simulator.storage import insert_operational_record, insert_private_record, utc_timestamp
from simulator.transactions import (
    LEGITIMATE_NEW_ACCOUNTS_METADATA_KEY,
    TransactionSummary,
    card_token_for_customer,
    device_id_for_transaction,
    generate_episode_transactions,
    generate_transactions,
)
from simulator.volume import (
    ReferenceWindow,
    VolumeBaselines,
    injected_transaction_count,
    load_reference_window,
    reference_volume_baselines,
)
from simulator.world import (
    ACCOUNT_PROFILE_METADATA_KEY,
    WorldSummary,
    generate_world,
)


EPISODE_SEED_DERIVATION = (
    "smoke: sha256-keyed p1.7.episode.v1(episode_id); "
    "pilot: sha256-keyed p1.7.episode.v2(profile, episode_id); reduced modulo 2^63"
)
LEGITIMATE_NEW_ACCOUNT_METADATA_PREFIX = "p1.7.legitimate_new_accounts.v1:"
EpisodeTemplate = Literal[
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


@dataclass(frozen=True)
class GeneratedEpisode:
    episode_id: str
    role: str
    seed: int
    template: EpisodeTemplate
    content_fingerprint: str
    config_fingerprint: str
    transaction_summary: TransactionSummary
    fraud_summary: FraudSummary
    label_summary: LabelSummary
    incident_summary: IncidentSummary | tuple[IncidentSummary, IncidentSummary] | None


@dataclass(frozen=True)
class EpisodeGenerationSummary:
    base_world: WorldSummary
    base_transaction_summary: TransactionSummary
    base_fraud_summary: FraudSummary
    base_label_summary: LabelSummary
    episodes: tuple[GeneratedEpisode, ...]


def derive_episode_seed(
    root_seed: int, episode_id: str, profile: ProfileName = "smoke"
) -> int:
    """Preserve legacy smoke seeds and scope new pilot seeds to their profile."""
    if not isinstance(episode_id, str) or not episode_id.strip() or episode_id == "base":
        raise ValueError("episode_id must identify a non-base episode")
    if profile not in {"smoke", "pilot"}:
        raise ValueError("profile must be 'smoke' or 'pilot'")
    if profile == "smoke":
        return derive_seed(root_seed, "p1.7.episode.v1", episode_id) % (2**63)
    return derive_seed(root_seed, "p1.7.episode.v2", profile, episode_id) % (2**63)


def _episode_config_snapshot(
    simulator_config: SimulatorConfig,
    episode_config: EpisodeConfig,
    assignment: EpisodeAssignment,
    episode_seed: int,
    profile: ProfileName,
) -> dict[str, object]:
    template_config = None
    if assignment.template == "data_pipeline":
        template_config = episode_config.templates.data_pipeline.model_dump(mode="json")
    elif assignment.template == "seasonal":
        template_config = episode_config.templates.seasonal.model_dump(mode="json")
    elif assignment.template == "fraud_surge":
        template_config = episode_config.templates.fraud_surge.model_dump(mode="json")
    elif assignment.template == "fraud_calendar":
        template_config = {
            "date": episode_config.templates.fraud_calendar.model_dump(mode="json"),
            "calendar": episode_config.templates.seasonal.model_dump(mode="json"),
            "fraud": episode_config.templates.fraud_surge.model_dump(mode="json"),
        }
    elif assignment.template == "two_cause_fraud_seasonal":
        template_config = {
            "fraud": episode_config.templates.fraud_surge.model_dump(mode="json"),
            "seasonal": episode_config.templates.seasonal.model_dump(mode="json"),
        }
    elif assignment.template == "new_merchant_sparse_history":
        template_config = episode_config.templates.new_merchant_sparse_history.model_dump(
            mode="json"
        )
    elif assignment.template == "silent_concept_drift":
        template_config = episode_config.templates.silent_concept_drift.model_dump(mode="json")
    return {
        "schema_version": 1,
        "generation_profile": profile,
        "simulator_config": simulator_config.model_dump(mode="json"),
        "episode_config": {
            "schema_version": episode_config.schema_version,
            "root_seed": episode_config.seed.model_dump(mode="json"),
            "protocol": episode_config.protocol.model_dump(mode="json"),
            "assignment": assignment.model_dump(mode="json"),
            "template_config": template_config,
            "n7_controls": episode_config.templates.legitimate_new_account_controls.model_dump(
                mode="json"
            ),
        },
        "seed_derivation": EPISODE_SEED_DERIVATION,
        "episode_seed": episode_seed,
    }


def seasonal_injection_count(
    simulator_config: SimulatorConfig, episode_config: EpisodeConfig, volume: VolumeBaselines
) -> int:
    """Seasonal traffic on the event channel: floor or the amplitude times that channel's median."""
    seasonal = episode_config.templates.seasonal
    return injected_transaction_count(
        minimum=seasonal.additional_transaction_count.value,
        relative_amplitude=seasonal.relative_amplitude.value,
        baseline=volume.channel(simulator_config.transactions.channel.value),
    )


def fraud_surge_injection_count(
    simulator_config: SimulatorConfig, episode_config: EpisodeConfig, volume: VolumeBaselines
) -> int:
    """Card-testing burst rows: the smoke burst size or the amplitude times the total median."""
    return injected_transaction_count(
        minimum=card_testing_burst_size(simulator_config),
        relative_amplitude=episode_config.templates.fraud_surge.relative_amplitude.value,
        baseline=volume.total,
    )


def _inject_template(
    operational: sqlite3.Connection,
    private: sqlite3.Connection,
    simulator_config: SimulatorConfig,
    episode_config: EpisodeConfig,
    assignment: EpisodeAssignment,
    episode_id: str,
    seed: int,
    world_summary: WorldSummary,
    transaction_random_keys: dict[str, str],
    volume: VolumeBaselines,
) -> IncidentSummary | tuple[IncidentSummary, IncidentSummary] | None:
    if assignment.template == "clean":
        return None
    if assignment.template == "data_pipeline":
        return inject_data_pipeline(
            operational,
            private,
            simulator_config,
            episode_id=episode_id,
            incident_truth_id=f"truth-{episode_id}-data-pipeline",
            mode=episode_config.templates.data_pipeline.mode.value,
        )
    t0 = simulator_config.timeline.t0_business_date.value
    seasonal = episode_config.templates.seasonal
    event_channel = simulator_config.transactions.channel.value
    if assignment.template == "seasonal":
        business_date = t0 + timedelta(days=seasonal.date_offset_days.value)
        return inject_seasonal(
            operational,
            private,
            simulator_config,
            episode_id=episode_id,
            incident_truth_id=f"truth-{episode_id}-seasonal",
            business_date=business_date,
            event_name=seasonal.event_name.value,
            additional_transaction_count=seasonal_injection_count(
                simulator_config, episode_config, volume
            ),
            calendar_scope_baseline=volume.channel(event_channel),
            seed=seed,
            customer_ids=world_summary.customer_ids,
            transaction_random_keys=transaction_random_keys,
        )
    if assignment.template == "fraud_surge":
        business_date = t0 + timedelta(
            days=episode_config.templates.fraud_surge.date_offset_days.value
        )
        return inject_fraud_surge(
            operational,
            private,
            simulator_config,
            episode_id=episode_id,
            incident_truth_id=f"truth-{episode_id}-fraud-surge",
            business_date=business_date,
            seed=seed,
            transaction_random_keys=transaction_random_keys,
            injected_transaction_count=fraud_surge_injection_count(
                simulator_config, episode_config, volume
            ),
        )
    if assignment.template == "fraud_calendar":
        business_date = t0 + timedelta(
            days=episode_config.templates.fraud_calendar.date_offset_days.value
        )
        return inject_fraud_surge(
            operational,
            private,
            simulator_config,
            episode_id=episode_id,
            incident_truth_id=f"truth-{episode_id}-fraud-calendar",
            business_date=business_date,
            seed=seed,
            transaction_random_keys=transaction_random_keys,
            injected_transaction_count=fraud_surge_injection_count(
                simulator_config, episode_config, volume
            ),
            calendar_event_name=seasonal.event_name.value,
            calendar_channel=event_channel,
            calendar_scope_baseline=volume.channel(event_channel),
        )
    if assignment.template == "two_cause_fraud_seasonal":
        business_date = t0 + timedelta(days=seasonal.date_offset_days.value)
        return inject_two_cause_fraud_seasonal(
            operational,
            private,
            simulator_config,
            episode_id=episode_id,
            fraud_truth_id=f"truth-{episode_id}-fraud-surge",
            seasonal_truth_id=f"truth-{episode_id}-seasonal",
            parent_scenario=f"scenario-{episode_id}",
            business_date=business_date,
            event_name=seasonal.event_name.value,
            seasonal_additional_transaction_count=seasonal_injection_count(
                simulator_config, episode_config, volume
            ),
            seasonal_calendar_scope_baseline=volume.channel(event_channel),
            fraud_injected_transaction_count=fraud_surge_injection_count(
                simulator_config, episode_config, volume
            ),
            seed=seed,
            customer_ids=world_summary.customer_ids,
            transaction_random_keys=transaction_random_keys,
        )
    if assignment.template == "new_fraud":
        return inject_new_fraud(
            operational,
            private,
            simulator_config,
            episode_id=episode_id,
            incident_truth_id=f"truth-{episode_id}-new-fraud",
            seed=seed,
            transaction_random_keys=transaction_random_keys,
        )
    if assignment.template == "legit_business_spike":
        return inject_legit_business_spike(
            operational,
            private,
            simulator_config,
            episode_id=episode_id,
            incident_truth_id=f"truth-{episode_id}-business-spike",
            seed=seed,
            transaction_random_keys=transaction_random_keys,
        )
    if assignment.template == "new_merchant_sparse_history":
        template = episode_config.templates.new_merchant_sparse_history
        return inject_sparse_new_merchant(
            operational,
            private,
            simulator_config,
            episode_id=episode_id,
            incident_truth_id=f"truth-{episode_id}-new-merchant",
            truth_cause="no_material_change",
            business_date=t0 + timedelta(days=template.date_offset_days.value),
            transaction_count=template.transaction_count.value,
            seed=seed,
            transaction_random_keys=transaction_random_keys,
        )
    if assignment.template == "silent_concept_drift":
        template = episode_config.templates.silent_concept_drift
        return inject_silent_concept_drift(
            operational,
            private,
            simulator_config,
            episode_id=episode_id,
            incident_truth_id=f"truth-{episode_id}-silent-drift",
            business_date=t0 + timedelta(days=template.date_offset_days.value),
            transaction_count=template.transaction_count.value,
            seed=seed,
            transaction_random_keys=transaction_random_keys,
        )
    raise ValueError(f"unsupported locked episode template {assignment.template!r}")


def _inject_legitimate_new_account_controls(
    operational: sqlite3.Connection,
    private: sqlite3.Connection,
    simulator_config: SimulatorConfig,
    episode_config: EpisodeConfig,
    assignment: EpisodeAssignment,
    *,
    episode_id: str,
    seed: int,
    transaction_random_keys: dict[str, str],
) -> None:
    """Add neutral, private-tracked legitimate customers to every episode."""
    controls = episode_config.templates.legitimate_new_account_controls
    timezone = ZoneInfo(simulator_config.timeline.business_timezone.value)
    t0_date = simulator_config.timeline.t0_business_date.value
    has_social_engineering_mule = assignment.template == "new_fraud"
    matching_account_count = (
        0
        if has_social_engineering_mule
        else controls.additional_accounts_without_social_engineering.value
    )
    matching_customer_schedule = None
    if matching_account_count:
        social = simulator_config.fraud.social_engineering
        matching_campaign_date = t0_date + timedelta(days=social.start_offset_days.value)
        matching_customer_schedule = social_engineering_customer_schedule(
            simulator_config, seed, episode_id, matching_campaign_date, 0
        )
    profile_rows = [
        dict(row)
        for row in operational.execute(
            "SELECT segment, province, age_band FROM customers "
            "WHERE episode_id = 'base' ORDER BY customer_id"
        )
    ]
    world_merchants = tuple(
        row[0]
        for row in operational.execute(
            "SELECT merchant_id FROM merchants WHERE episode_id = 'base' ORDER BY merchant_id"
        )
    )
    if not profile_rows or not world_merchants:
        raise ValueError("legitimate new-account controls require base customer and merchant history")
    occupied = {
        row[0] for row in operational.execute("SELECT customer_id FROM customers")
    }
    account_rows: list[dict[str, object]] = []
    account_count = controls.accounts_per_episode.value + matching_account_count
    for account_index in range(account_count):
        is_matching_account = account_index >= controls.accounts_per_episode.value
        if is_matching_account:
            assert matching_customer_schedule is not None
            account_creation_at, matching_first_transaction_at = matching_customer_schedule
            purpose = "timing_match"
            transaction_count = controls.timing_match_transactions_per_account.value
        else:
            if account_index == 0:
                purpose = "early_control"
                transaction_count = controls.early_transactions_per_account.value
                offset_days = controls.creation_offset_days.value
            else:
                purpose = "in_window_control"
                transaction_count = controls.transactions_per_account.value
                offset_days = controls.creation_in_window_offset_days.value + keyed_rng(
                    seed,
                    "p1.7.legitimate_new_account.creation_date",
                    episode_id,
                    account_index,
                ).randint(0, controls.creation_in_window_jitter_days.value)
            creation_time_seconds = keyed_rng(
                seed,
                "p1.7.legitimate_new_account.creation_time",
                episode_id,
                account_index,
            ).randint(1, controls.creation_time_jitter_seconds.value)
            account_creation_at = datetime.combine(
                t0_date + timedelta(days=offset_days), time.min, tzinfo=timezone
            ) + timedelta(seconds=creation_time_seconds)
            matching_first_transaction_at = None
        profile_rng = keyed_rng(
            seed, "p1.7.legitimate_new_account.profile", episode_id, account_index
        )
        profile = profile_rng.choice(profile_rows)
        customer = generate_customer_id(
            seed,
            "p1.7.legitimate_new_account.customer",
            episode_id,
            account_index,
            occupied=occupied,
        )
        insert_operational_record(
            operational,
            "customers",
            {
                "customer_id": customer,
                "episode_id": episode_id,
                "event_at": account_creation_at,
                "available_at": account_creation_at,
                "segment": profile["segment"],
                "province": profile["province"],
                "age_band": profile["age_band"],
            },
        )
        transaction_ids: list[str] = []
        for transaction_index in range(transaction_count):
            field_rng = keyed_rng(
                seed,
                "p1.7.legitimate_new_account.transaction_fields",
                episode_id,
                customer,
                transaction_index,
            )
            hour_weights = simulator_config.transactions.hourly_transaction_weights.value[:22]
            hour = (
                0
                if account_index == 0
                else field_rng.choices(range(22), weights=hour_weights, k=1)[0]
            )
            if is_matching_account:
                assert matching_first_transaction_at is not None
                created_at = matching_first_transaction_at + timedelta(days=transaction_index)
            else:
                first_transaction_offset = (
                    controls.early_first_transaction_offset_days.value
                    if account_index == 0
                    else controls.first_transaction_offset_days.value
                )
                created_at = datetime.combine(
                    t0_date + timedelta(days=first_transaction_offset + transaction_index),
                    time(hour, field_rng.randrange(60), field_rng.randint(1, 59)),
                    tzinfo=timezone,
                )
            if created_at - account_creation_at < timedelta(
                days=controls.minimum_account_age_days.value
            ):
                raise ValueError("legitimate new-account transaction starts before its history window")
            merchant_id = field_rng.choice(world_merchants)
            channel_names = tuple(sorted(simulator_config.transactions.channel_weights.value))
            channel_weights = [
                simulator_config.transactions.channel_weights.value[name]
                for name in channel_names
            ]
            channel = field_rng.choices(channel_names, weights=channel_weights, k=1)[0]
            amount = field_rng.randint(
                simulator_config.transactions.minimum_amount.value,
                simulator_config.transactions.maximum_amount.value,
            )
            txn_id = transaction_id(
                seed,
                "p1.7.legitimate_new_account.transaction",
                episode_id,
                customer,
                transaction_index,
            )
            label_key = f"txn-{keyed_rng(seed, 'p1.7.legitimate_new_account.label_key', episode_id, customer, transaction_index).getrandbits(128):032x}"
            transaction_random_keys[txn_id] = label_key
            insert_operational_record(
                operational,
                "transactions",
                {
                    "txn_id": txn_id,
                    "episode_id": episode_id,
                    "customer_id": customer,
                    "merchant_id": merchant_id,
                    "recipient_customer_id": None,
                    "device_id": device_id_for_transaction(
                        identity_seed=simulator_config.seed.value,
                        draw_seed=seed,
                        episode_id=episode_id,
                        customer_id=customer,
                        transaction_key=label_key,
                        change_probability=(
                            simulator_config.world.legitimate_device_change_probability.value
                        ),
                    ),
                    "card_token": card_token_for_customer(simulator_config.seed.value, customer)
                    if channel == "card"
                    else None,
                    "created_at": created_at,
                    "event_at": created_at,
                    "available_at": created_at,
                    "amount": amount,
                    "channel": channel,
                },
            )
            authorized_at = created_at + timedelta(
                seconds=simulator_config.transactions.authorization_delay_seconds.value
            )
            settled_at = authorized_at + timedelta(
                minutes=simulator_config.transactions.settlement_delay_minutes.value
            )
            insert_operational_record(
                operational,
                "transaction_events",
                {
                    "event_id": f"{txn_id}:authorized",
                    "txn_id": txn_id,
                    "episode_id": episode_id,
                    "event_type": "authorized",
                    "event_at": authorized_at,
                    "available_at": authorized_at,
                    "response_code": simulator_config.transactions.authorization_response_code.value,
                },
            )
            insert_operational_record(
                operational,
                "transaction_events",
                {
                    "event_id": f"{txn_id}:settled",
                    "txn_id": txn_id,
                    "episode_id": episode_id,
                    "event_type": "settled",
                    "event_at": settled_at,
                    "available_at": settled_at,
                    "response_code": None,
                },
            )
            transaction_ids.append(txn_id)
        account_rows.append(
            {
                "customer_id": customer,
                "role": "individual",
                "purpose": purpose,
                "created_at": utc_timestamp(account_creation_at),
                "transaction_ids": transaction_ids,
            }
        )
    insert_private_record(
        private,
        "run_metadata",
        {
            "key": f"{LEGITIMATE_NEW_ACCOUNT_METADATA_PREFIX}{episode_id}",
            "value_json": {
                "algorithm": "p1.7-legitimate-new-account-controls-v2",
                "episode_id": episode_id,
                "seed": seed,
                "accounts": account_rows,
            },
        },
    )


def generate_episodes(
    operational: sqlite3.Connection,
    private: sqlite3.Connection,
    simulator_config: SimulatorConfig,
    episode_config: EpisodeConfig,
    *,
    profile: ProfileName = "smoke",
    volume_window: ReferenceWindow | None = None,
) -> EpisodeGenerationSummary:
    """Generate one shared base history and a profile's locked episodes.

    A fresh store pair is required. The base world and pre-T0 data are inserted
    once. Each episode then receives an ID-derived seed and an isolated 30-day
    future; only its own role is exposed in the operational ``episodes`` table.
    Count-scaled templates read their reference medians from the shared base history
    over ``volume_window`` (default: the monitoring reference in configs/monitoring.yaml).
    """
    if operational is private:
        raise ValueError("episode generation requires separate operational and private stores")
    base_row = operational.execute(
        "SELECT role FROM episodes WHERE episode_id = 'base'"
    ).fetchone()
    if base_row is None or base_row[0] != "base":
        raise ValueError("episode generation requires the base sentinel row")
    if any(
        operational.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone()
        for table in ("customers", "merchants", "transactions", "transaction_events", "label_events", "business_calendar")
    ):
        raise ValueError("episode generation requires an empty operational data store")
    if any(
        private.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone()
        for table in ("private_episodes", "episode_manifest", "run_metadata", "incident_truth")
    ):
        raise ValueError("episode generation requires an empty private data store")

    assignments = tuple(sorted(episode_config.profile(profile).assignments, key=lambda item: item.episode_id))
    if not assignments:
        raise ValueError("smoke profile has no locked episode assignments")
    duration_days = episode_config.protocol.duration_days.value
    t0_local = datetime.combine(
        simulator_config.timeline.t0_business_date.value, time.min
    ).replace(tzinfo=ZoneInfo(simulator_config.timeline.business_timezone.value))

    operational.execute("SAVEPOINT p1_7_episode_run")
    private.execute("SAVEPOINT p1_7_episode_run")
    try:
        base_world = generate_world(operational, simulator_config, profile)
        transaction_random_keys: dict[str, str] = {}
        insert_private_record(
            private,
            "run_metadata",
            {
                "key": ACCOUNT_PROFILE_METADATA_KEY,
                "value_json": base_world.private_metadata(),
            },
        )
        base_transactions = generate_transactions(
            operational,
            simulator_config,
            profile,
            world_summary=base_world,
            transaction_random_keys=transaction_random_keys,
        )
        insert_private_record(
            private,
            "run_metadata",
            {
                "key": LEGITIMATE_NEW_ACCOUNTS_METADATA_KEY,
                "value_json": {
                    "episode_id": "base",
                    "seed": simulator_config.seed.value,
                    "customer_ids": list(base_transactions.legitimate_new_customer_ids),
                    "count": len(base_transactions.legitimate_new_customer_ids),
                },
            },
        )
        base_fraud = generate_card_testing(
            operational,
            private,
            simulator_config,
            seed=simulator_config.seed.value,
            episode_id="base",
            customer_ids=base_world.customer_ids,
            transaction_random_keys=transaction_random_keys,
        )
        generate_other_fraud_mechanisms(
            operational,
            private,
            simulator_config,
            base_world,
            seed=simulator_config.seed.value,
            episode_id="base",
            mechanisms={"ato", "collusion", "household_business_activity"},
            transaction_random_keys=transaction_random_keys,
        )
        base_labels = generate_label_events(
            operational,
            private,
            simulator_config,
            seed=simulator_config.seed.value,
            episode_id="base",
            transaction_random_keys=transaction_random_keys,
        )
        volume = reference_volume_baselines(
            operational,
            simulator_config,
            volume_window if volume_window is not None else load_reference_window(),
        )

        generated: list[GeneratedEpisode] = []
        for assignment in assignments:
            episode_id = assignment.episode_id
            episode_seed = derive_episode_seed(episode_config.seed.value, episode_id, profile)
            if operational.execute(
                "SELECT 1 FROM episodes WHERE episode_id = ?", (episode_id,)
            ).fetchone() is not None:
                raise ValueError(f"episode role is already registered and locked: {episode_id!r}")
            if private.execute(
                "SELECT 1 FROM private_episodes WHERE episode_id = ?", (episode_id,)
            ).fetchone() is not None:
                raise ValueError(f"episode manifest is already registered and locked: {episode_id!r}")

            insert_operational_record(
                operational,
                "episodes",
                {"episode_id": episode_id, "role": assignment.role},
            )
            insert_private_record(private, "private_episodes", {"episode_id": episode_id})

            transaction_summary = generate_episode_transactions(
                operational,
                simulator_config,
                profile,
                episode_id=episode_id,
                seed=episode_seed,
                duration_days=duration_days,
                world_summary=base_world,
                transaction_random_keys=transaction_random_keys,
            )
            fraud_summary = generate_card_testing(
                operational,
                private,
                simulator_config,
                seed=episode_seed,
                episode_id=episode_id,
                duration_days=duration_days,
                customer_ids=base_world.customer_ids,
                transaction_random_keys=transaction_random_keys,
            )
            generate_other_fraud_mechanisms(
                operational,
                private,
                simulator_config,
                base_world,
                seed=episode_seed,
                episode_id=episode_id,
                duration_days=duration_days,
                include_episode_mechanisms=assignment.template != "clean",
                mechanisms={"ato"} if assignment.template != "clean" else set(),
                transaction_random_keys=transaction_random_keys,
            )
            incident_summary = _inject_template(
                operational,
                private,
                simulator_config,
                episode_config,
                assignment,
                episode_id,
                episode_seed,
                base_world,
                transaction_random_keys,
                volume,
            )
            # Preserve the P1.6 effect scope. N7 activity is legitimate data
            # arriving after the template event, not part of its injected rows.
            _inject_legitimate_new_account_controls(
                operational,
                private,
                simulator_config,
                episode_config,
                assignment,
                episode_id=episode_id,
                seed=episode_seed,
                transaction_random_keys=transaction_random_keys,
            )
            label_summary = generate_label_events(
                operational,
                private,
                simulator_config,
                seed=episode_seed,
                episode_id=episode_id,
                transaction_random_keys=transaction_random_keys,
            )

            config_snapshot = _episode_config_snapshot(
                simulator_config, episode_config, assignment, episode_seed, profile
            )
            config_json = json.dumps(
                config_snapshot,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            config_fingerprint = hashlib.sha256(config_json.encode("utf-8")).hexdigest()
            content_fingerprint = canonical_episode_fingerprint(
                operational,
                episode_id=episode_id,
                t0=t0_local.isoformat(),
            )
            insert_private_record(
                private,
                "episode_manifest",
                {
                    "episode_id": episode_id,
                    "role": assignment.role,
                    "seed": episode_seed,
                    "template": assignment.template,
                    "content_fingerprint": content_fingerprint,
                    "config_fingerprint": config_fingerprint,
                    "config_json": config_json,
                },
            )
            generated.append(
                GeneratedEpisode(
                    episode_id=episode_id,
                    role=assignment.role,
                    seed=episode_seed,
                    template=assignment.template,
                    content_fingerprint=content_fingerprint,
                    config_fingerprint=config_fingerprint,
                    transaction_summary=transaction_summary,
                    fraud_summary=fraud_summary,
                    label_summary=label_summary,
                    incident_summary=incident_summary,
                )
            )

        private.execute("RELEASE SAVEPOINT p1_7_episode_run")
        operational.execute("RELEASE SAVEPOINT p1_7_episode_run")
    except Exception:
        private.execute("ROLLBACK TO SAVEPOINT p1_7_episode_run")
        private.execute("RELEASE SAVEPOINT p1_7_episode_run")
        operational.execute("ROLLBACK TO SAVEPOINT p1_7_episode_run")
        operational.execute("RELEASE SAVEPOINT p1_7_episode_run")
        raise

    return EpisodeGenerationSummary(
        base_world=base_world,
        base_transaction_summary=base_transactions,
        base_fraud_summary=base_fraud,
        base_label_summary=base_labels,
        episodes=tuple(generated),
    )
