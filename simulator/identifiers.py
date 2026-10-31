"""Opaque, stable identifiers for generated operational entities."""

from __future__ import annotations

import hashlib
import json


def transaction_id(
    seed: int,
    namespace: str,
    episode_id: str,
    *keys: object,
) -> str:
    """Return the single operational transaction ID shape used by every stream."""
    payload = json.dumps(
        ["payguard-transaction-id-v2", seed, namespace, episode_id, *keys],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"txn-{hashlib.sha256(payload).hexdigest()}"


def customer_id(
    seed: int,
    namespace: str,
    episode_id: str,
    *keys: object,
    occupied: set[str],
) -> str:
    """Return an opaque six-digit customer ID, resolving rare collisions deterministically."""
    collision_index = 0
    while collision_index < 1_000_000:
        payload = json.dumps(
            [
                "payguard-customer-id-v2",
                seed,
                namespace,
                episode_id,
                *keys,
                collision_index,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        candidate = int(hashlib.sha256(payload).hexdigest()[:16], 16) % 1_000_000
        identifier = f"customer-{candidate:06d}"
        if identifier not in occupied:
            occupied.add(identifier)
            return identifier
        collision_index += 1
    raise ValueError("unable to allocate a unique synthetic customer ID")
