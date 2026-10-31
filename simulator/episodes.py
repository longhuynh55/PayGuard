"""Generate the P1.7 round-one smoke episode set and private manifest."""

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
)
from simulator.incidents import IncidentSummary, inject_data_pipeline, inject_seasonal
from simulator.labels import LabelSummary, generate_label_events
from simulator.randomness import derive_seed
from simulator.reproducibility import canonical_episode_fingerprint
from simulator.storage import insert_operational_record, insert_private_record
from simulator.transactions import (
    LEGITIMATE_NEW_ACCOUNTS_METADATA_KEY,
    TransactionSummary,
    generate_episode_transactions,
    generate_transactions,
)
from simulator.world import (
    ACCOUNT_PROFILE_METADATA_KEY,
    WorldSummary,
    generate_world,
)


EPISODE_SEED_DERIVATION = "sha256-keyed p1.7.episode.v1, reduced modulo 2^63"
EpisodeTemplate = Literal["clean", "data_pipeline", "seasonal"]


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
    incident_summary: IncidentSummary | None


@dataclass(frozen=True)
class EpisodeGenerationSummary:
    base_world: WorldSummary
    base_transaction_summary: TransactionSummary
    base_fraud_summary: FraudSummary
    base_label_summary: LabelSummary
    episodes: tuple[GeneratedEpisode, ...]


def derive_episode_seed(root_seed: int, episode_id: str) -> int:
    """Derive a stable SQLite-safe seed from the root seed and episode ID only."""
    if not isinstance(episode_id, str) or not episode_id.strip() or episode_id == "base":
        raise ValueError("episode_id must identify a non-base episode")
    return derive_seed(root_seed, "p1.7.episode.v1", episode_id) % (2**63)


def _episode_config_snapshot(
    simulator_config: SimulatorConfig,
    episode_config: EpisodeConfig,
    assignment: EpisodeAssignment,
    episode_seed: int,
) -> dict[str, object]:
    template_config = None
    if assignment.template == "data_pipeline":
        template_config = episode_config.templates.data_pipeline.model_dump(mode="json")
    elif assignment.template == "seasonal":
        template_config = episode_config.templates.seasonal.model_dump(mode="json")
    return {
        "schema_version": 1,
        "generation_profile": "smoke",
        "simulator_config": simulator_config.model_dump(mode="json"),
        "episode_config": {
            "schema_version": episode_config.schema_version,
            "root_seed": episode_config.seed.model_dump(mode="json"),
            "protocol": episode_config.protocol.model_dump(mode="json"),
            "assignment": assignment.model_dump(mode="json"),
            "template_config": template_config,
        },
        "seed_derivation": EPISODE_SEED_DERIVATION,
        "episode_seed": episode_seed,
    }


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
) -> IncidentSummary | None:
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

    business_date = (
        simulator_config.timeline.t0_business_date.value
        + timedelta(days=episode_config.templates.seasonal.date_offset_days.value)
    )
    return inject_seasonal(
        operational,
        private,
        simulator_config,
        episode_id=episode_id,
        incident_truth_id=f"truth-{episode_id}-seasonal",
        business_date=business_date,
        event_name=episode_config.templates.seasonal.event_name.value,
        additional_transaction_count=(
            episode_config.templates.seasonal.additional_transaction_count.value
        ),
        expected_relative_change=(
            episode_config.templates.seasonal.expected_relative_change.value
        ),
        seed=seed,
        customer_ids=world_summary.customer_ids,
        transaction_random_keys=transaction_random_keys,
    )


def generate_episodes(
    operational: sqlite3.Connection,
    private: sqlite3.Connection,
    simulator_config: SimulatorConfig,
    episode_config: EpisodeConfig,
    *,
    profile: ProfileName = "smoke",
) -> EpisodeGenerationSummary:
    """Generate one shared base history and the locked round-one smoke episodes.

    A fresh store pair is required. The base world and pre-T0 data are inserted
    once. Each episode then receives an ID-derived seed and an isolated 30-day
    future; only its own role is exposed in the operational ``episodes`` table.
    """
    if operational is private:
        raise ValueError("episode generation requires separate operational and private stores")
    if profile != "smoke":
        raise ValueError("P1.7 round one generates the smoke profile only")
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

        generated: list[GeneratedEpisode] = []
        for assignment in assignments:
            episode_id = assignment.episode_id
            episode_seed = derive_episode_seed(episode_config.seed.value, episode_id)
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
                simulator_config, episode_config, assignment, episode_seed
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
