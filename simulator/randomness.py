"""Stable, component-scoped pseudo-random streams for simulator generation."""

from __future__ import annotations

import hashlib
import json
import random
from typing import Any


def derive_seed(seed: int, component: str, *keys: Any) -> int:
    """Derive a deterministic seed without coupling component draw order."""
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed must be a non-negative integer")
    if not component.strip():
        raise ValueError("component must not be blank")
    payload = json.dumps(
        ["payguard-rng-v1", seed, component, *keys],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest(), "big")


def keyed_rng(seed: int, component: str, *keys: Any) -> random.Random:
    """Return a repeatable RNG for one component and optional stable key."""
    return random.Random(derive_seed(seed, component, *keys))
