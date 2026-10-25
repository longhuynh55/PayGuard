from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from simulator.storage import StorePair, create_export_stores, insert_operational_record


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "p1_2_operational.json"


def load_operational_fixture(pair: StorePair) -> dict[str, Any]:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    for table in (
        "episodes",
        "customers",
        "merchants",
        "transactions",
        "transaction_events",
        "label_events",
        "business_calendar",
    ):
        for row in fixture[table]:
            insert_operational_record(pair.operational, table, row)
    return fixture


@pytest.fixture
def p1_2_store(tmp_path: Path):
    pair = create_export_stores(tmp_path / "p1_2-run")
    try:
        load_operational_fixture(pair)
        pair.operational.commit()
        yield pair
    finally:
        pair.close()
