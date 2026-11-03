"""Contract of the shared timestamp and canonical-JSON helpers used across packages."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from simulator.common import (
    TimestampParseError,
    canonical_json,
    canonical_sha256,
    parse_timestamp,
)


def test_timestamps_are_read_in_utc_and_naive_or_non_utc_values_are_rejected() -> None:
    expected = datetime(2026, 2, 1, tzinfo=UTC)
    assert parse_timestamp("2026-02-01T00:00:00Z") == expected
    assert parse_timestamp("2026-02-01T07:00:00+07:00") == expected

    rejected = (
        ("2026-02-01T00:00:00", "naive"),
        ("not a timestamp", "invalid"),
        (None, "not_text"),
    )
    for value, reason in rejected:
        with pytest.raises(TimestampParseError) as caught:
            parse_timestamp(value)
        assert caught.value.reason == reason

    with pytest.raises(TimestampParseError) as caught:
        parse_timestamp("2026-02-01T07:00:00+07:00", utc=True)
    assert caught.value.reason == "not_utc"


def test_canonical_json_sorts_keys_compacts_separators_and_rejects_non_finite_numbers() -> None:
    assert canonical_json({"b": 1, "a": "é"}) == '{"a":"é","b":1}'
    assert canonical_sha256({"b": 1, "a": "é"}) == canonical_sha256({"a": "é", "b": 1})
    for value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            canonical_json({"x": value})
