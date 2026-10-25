"""Generate the shared pre-T0 customer and merchant world."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

from simulator.config import ProfileName, SimulatorConfig
from simulator.randomness import keyed_rng
from simulator.storage import insert_operational_record


@dataclass(frozen=True)
class WorldSummary:
    customer_ids: tuple[str, ...]
    merchant_ids: tuple[str, ...]


def generate_world(
    connection: sqlite3.Connection,
    config: SimulatorConfig,
    profile: ProfileName = "smoke",
    *,
    seed: int | None = None,
) -> WorldSummary:
    """Insert base customers and merchants using the independent world RNG stream."""
    run_seed = config.seed.value if seed is None else seed
    profile_scale = config.scale_for(profile)
    start_date = config.timeline.start_business_date.value
    t0_date = config.timeline.t0_business_date.value
    if start_date >= t0_date:
        raise ValueError("world generation requires at least one configured business day before T0")

    existing_customers = connection.execute("SELECT 1 FROM customers LIMIT 1").fetchone()
    existing_merchants = connection.execute("SELECT 1 FROM merchants LIMIT 1").fetchone()
    if existing_customers or existing_merchants:
        raise ValueError("world generation requires empty customers and merchants tables")

    business_tz = ZoneInfo(config.timeline.business_timezone.value)
    available_from = datetime.combine(start_date, time.min).replace(tzinfo=business_tz)
    world = config.world
    customer_ids: list[str] = []
    merchant_ids: list[str] = []

    for index in range(1, profile_scale.customer_count.value + 1):
        customer_id = f"customer-{index:06d}"
        rng = keyed_rng(run_seed, "p1.world.customer", customer_id)
        insert_operational_record(
            connection,
            "customers",
            {
                "customer_id": customer_id,
                "episode_id": "base",
                "event_at": available_from,
                "available_at": available_from,
                "segment": rng.choice(world.customer_segments.value),
                "province": rng.choice(world.customer_provinces.value),
                "age_band": rng.choice(world.customer_age_bands.value),
            },
        )
        customer_ids.append(customer_id)

    for index in range(1, profile_scale.merchant_count.value + 1):
        merchant_id = f"merchant-{index:06d}"
        rng = keyed_rng(run_seed, "p1.world.merchant", merchant_id)
        insert_operational_record(
            connection,
            "merchants",
            {
                "merchant_id": merchant_id,
                "episode_id": "base",
                "event_at": available_from,
                "available_at": available_from,
                "mcc": rng.choice(world.merchant_mcc_codes.value),
                "province": rng.choice(world.merchant_provinces.value),
            },
        )
        merchant_ids.append(merchant_id)

    return WorldSummary(tuple(customer_ids), tuple(merchant_ids))
