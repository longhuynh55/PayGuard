"""Generate the shared pre-T0 customer and merchant world."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

from simulator.config import ProfileName, SimulatorConfig
from simulator.randomness import keyed_rng
from simulator.storage import insert_operational_record


ACCOUNT_PROFILE_METADATA_KEY = "p1.3.account_profiles.v1"


@dataclass(frozen=True)
class WorldSummary:
    customer_ids: tuple[str, ...]
    merchant_ids: tuple[str, ...]
    account_roles: dict[str, str]
    familiar_payees: dict[str, tuple[str, ...]]
    supplier_merchants: dict[str, tuple[str, ...]]
    internal_mule_outbound_window_hours: int

    def private_metadata(self) -> dict[str, object]:
        """Return role and relationship metadata that must stay outside operational data."""
        return {
            "schema_version": 1,
            "account_roles": self.account_roles,
            "familiar_payees": self.familiar_payees,
            "supplier_merchants": self.supplier_merchants,
            "internal_mule_outbound_window_hours": (
                self.internal_mule_outbound_window_hours
            ),
        }


def _account_profiles(
    customer_ids: tuple[str, ...],
    merchant_ids: tuple[str, ...],
    config: SimulatorConfig,
    *,
    seed: int,
) -> tuple[dict[str, str], dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
    weights = config.world.account_role_weights.value
    ordered_roles = tuple(sorted(weights))
    exact_counts = {role: len(customer_ids) * weights[role] for role in ordered_roles}
    counts = {role: int(exact_counts[role]) for role in ordered_roles}
    remaining = len(customer_ids) - sum(counts.values())
    for role in sorted(
        ordered_roles,
        key=lambda item: (-(exact_counts[item] - counts[item]), item),
    )[:remaining]:
        counts[role] += 1
    if len(customer_ids) >= len(ordered_roles):
        for role in ordered_roles:
            if counts[role] == 0:
                donor = max(ordered_roles, key=lambda item: (counts[item], item))
                counts[donor] -= 1
                counts[role] = 1

    assigned_roles = [
        role for role in ordered_roles for _ in range(counts[role])
    ]
    keyed_rng(seed, "p1.world.account_role_assignment").shuffle(assigned_roles)
    account_roles = dict(zip(customer_ids, assigned_roles, strict=True))

    household_ids = tuple(
        customer_id
        for customer_id in customer_ids
        if account_roles[customer_id] == "household_business"
    )
    individual_ids = tuple(
        customer_id
        for customer_id in customer_ids
        if account_roles[customer_id] == "individual"
    )
    familiar_payees: dict[str, tuple[str, ...]] = {}
    for index, customer_id in enumerate(individual_ids):
        candidates = [
            other
            for other in customer_ids
            if other != customer_id and account_roles[other] != "internal_mule"
        ]
        payee_rng = keyed_rng(seed, "p1.world.familiar_payees", customer_id)
        selected: list[str] = []
        if household_ids:
            selected.append(household_ids[index % len(household_ids)])
        remaining_candidates = [item for item in candidates if item not in selected]
        extra_count = min(
            config.world.familiar_account_payee_count.value - len(selected),
            len(remaining_candidates),
        )
        if extra_count > 0:
            selected.extend(payee_rng.sample(remaining_candidates, extra_count))
        familiar_payees[customer_id] = tuple(selected)

    supplier_merchants: dict[str, tuple[str, ...]] = {}
    for customer_id in household_ids:
        supplier_rng = keyed_rng(seed, "p1.world.supplier_merchants", customer_id)
        supplier_count = min(
            config.world.household_supplier_merchant_count.value,
            len(merchant_ids),
        )
        supplier_merchants[customer_id] = tuple(
            supplier_rng.sample(merchant_ids, supplier_count)
        )

    return account_roles, familiar_payees, supplier_merchants


def build_world_summary(
    customer_ids: tuple[str, ...],
    merchant_ids: tuple[str, ...],
    config: SimulatorConfig,
    *,
    seed: int,
) -> WorldSummary:
    account_roles, familiar_payees, supplier_merchants = _account_profiles(
        customer_ids,
        merchant_ids,
        config,
        seed=seed,
    )
    return WorldSummary(
        customer_ids,
        merchant_ids,
        account_roles,
        familiar_payees,
        supplier_merchants,
        config.world.internal_mule_outbound_window_hours.value,
    )


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

    return build_world_summary(
        tuple(customer_ids),
        tuple(merchant_ids),
        config,
        seed=run_seed,
    )
