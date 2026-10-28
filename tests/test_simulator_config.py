from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from simulator.config import (
    ConfigValidationError,
    load_episode_config,
    load_simulator_config,
    validate_episode_config,
    validate_episode_duration_match,
    validate_simulator_config,
)


ROOT = Path(__file__).resolve().parents[1]


def _yaml_mapping(path: Path) -> dict:
    with path.open(encoding="utf-8") as stream:
        return yaml.safe_load(stream)


def test_checked_in_configs_load_and_validate() -> None:
    simulator = load_simulator_config(ROOT / "configs" / "simulator.yaml")
    episodes = load_episode_config(ROOT / "configs" / "episodes.yaml")

    assert simulator.timeline.storage_timezone.value == "UTC"
    assert simulator.timeline.business_timezone.value == "Asia/Ho_Chi_Minh"
    assert simulator.episode.duration_days.value == 30
    assert simulator.transactions.minimum_amount.unit == "integer VND"
    assert episodes.protocol.duration_days.value == 30
    validate_episode_duration_match(simulator, episodes)


def test_load_episode_config_absolute_path_is_independent_of_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    episodes = load_episode_config(ROOT / "configs" / "episodes.yaml")

    assert episodes.protocol.duration_days.value == 30


def test_public_episode_duration_drift_is_rejected_and_protocol_match_is_checked() -> None:
    simulator_data = _yaml_mapping(ROOT / "configs" / "simulator.yaml")
    simulator_data["episode"]["duration_days"]["value"] = 29

    with pytest.raises(ConfigValidationError, match="episode duration_days is fixed at 30"):
        validate_simulator_config(simulator_data)

    simulator = load_simulator_config(ROOT / "configs" / "simulator.yaml")
    episodes = load_episode_config(ROOT / "configs" / "episodes.yaml")
    mismatched_duration = simulator.episode.duration_days.model_copy(update={"value": 29})
    mismatched_episode = simulator.episode.model_copy(
        update={"duration_days": mismatched_duration}
    )
    mismatched_simulator = simulator.model_copy(update={"episode": mismatched_episode})
    with pytest.raises(ConfigValidationError, match="must match episodes protocol.duration_days"):
        validate_episode_duration_match(mismatched_simulator, episodes)


def test_missing_required_fields_are_reported() -> None:
    simulator_data = _yaml_mapping(ROOT / "configs" / "simulator.yaml")
    del simulator_data["transactions"]["minimum_amount"]

    with pytest.raises(ConfigValidationError, match="transactions.minimum_amount"):
        validate_simulator_config(simulator_data)

    episode_data = _yaml_mapping(ROOT / "configs" / "episodes.yaml")
    del episode_data["protocol"]["duration_days"]

    with pytest.raises(ConfigValidationError, match="protocol.duration_days"):
        validate_episode_config(episode_data)


@pytest.mark.parametrize("probability", [-0.01, 1.01])
def test_probability_outside_unit_interval_is_rejected(probability: float) -> None:
    data = _yaml_mapping(ROOT / "configs" / "simulator.yaml")
    data["fraud"]["card_testing"]["approval_probability"]["value"] = probability

    with pytest.raises(ConfigValidationError, match="approval_probability.value"):
        validate_simulator_config(data)


def test_invalid_timeline_schedule_is_rejected() -> None:
    data = _yaml_mapping(ROOT / "configs" / "simulator.yaml")
    data["timeline"]["end_business_date_exclusive"]["value"] = "2025-07-01"

    with pytest.raises(ConfigValidationError, match="start_business_date must precede"):
        validate_simulator_config(data)


def test_episode_ids_must_not_encode_template_or_role() -> None:
    data = _yaml_mapping(ROOT / "configs" / "episodes.yaml")
    data["profiles"]["smoke"]["assignments"][1]["episode_id"] = (
        "smoke-pipeline-calibration"
    )

    with pytest.raises(ConfigValidationError, match="episode_id"):
        validate_episode_config(data)


def test_transaction_hourly_weights_must_cover_a_local_day() -> None:
    data = _yaml_mapping(ROOT / "configs" / "simulator.yaml")
    data["transactions"]["hourly_transaction_weights"]["value"] = [1.0] * 23

    with pytest.raises(ConfigValidationError, match="one value for each local hour"):
        validate_simulator_config(data)


def test_entity_profile_categories_must_be_unique() -> None:
    data = _yaml_mapping(ROOT / "configs" / "simulator.yaml")
    data["world"]["merchant_mcc_codes"]["value"] = ["5411", "5411"]

    with pytest.raises(ConfigValidationError, match="must not contain duplicates"):
        validate_simulator_config(data)


def test_lifecycle_delays_must_fit_inside_one_local_day() -> None:
    data = _yaml_mapping(ROOT / "configs" / "simulator.yaml")
    data["transactions"]["authorization_delay_seconds"]["value"] = 60
    data["transactions"]["settlement_delay_minutes"]["value"] = 1439

    with pytest.raises(ConfigValidationError, match="delays must total less than one day"):
        validate_simulator_config(data)


@pytest.mark.parametrize("horizon_days", [30, 60])
def test_expansion_label_horizons_are_rejected(horizon_days: int) -> None:
    data = _yaml_mapping(ROOT / "configs" / "simulator.yaml")
    data["label_reporting"]["maturity_horizon_days"]["value"] = horizon_days

    with pytest.raises(ConfigValidationError, match="maturity_horizon_days is fixed at 90"):
        validate_simulator_config(data)


def test_label_delay_distribution_is_explicit_and_supported() -> None:
    simulator = load_simulator_config(ROOT / "configs" / "simulator.yaml")
    assert simulator.label_reporting.delay_distribution.value == "uniform_integer"

    data = _yaml_mapping(ROOT / "configs" / "simulator.yaml")
    data["label_reporting"]["delay_distribution"]["value"] = "normal"
    with pytest.raises(ConfigValidationError, match="delay_distribution.value"):
        validate_simulator_config(data)


@pytest.mark.parametrize(
    ("timezone_field", "invalid_value"),
    [
        ("storage_timezone", "America/New_York"),
        ("business_timezone", "UTC"),
    ],
)
def test_timezones_outside_the_mvp_contract_are_rejected(
    timezone_field: str, invalid_value: str
) -> None:
    data = _yaml_mapping(ROOT / "configs" / "simulator.yaml")
    data["timeline"][timezone_field]["value"] = invalid_value

    with pytest.raises(ConfigValidationError, match=f"{timezone_field} is fixed"):
        validate_simulator_config(data)


@pytest.mark.parametrize("unit", ["USD", "nghìn VND", "triệu VND"])
def test_non_integer_vnd_amount_units_are_rejected(unit: str) -> None:
    data = _yaml_mapping(ROOT / "configs" / "simulator.yaml")
    data["transactions"]["minimum_amount"]["unit"] = unit
    data["transactions"]["maximum_amount"]["unit"] = unit

    with pytest.raises(ConfigValidationError, match="amount unit must be exactly 'integer VND'"):
        validate_simulator_config(data)


@pytest.mark.parametrize("missing_key", ["unit", "provenance"])
def test_parameter_without_unit_or_provenance_is_rejected(missing_key: str) -> None:
    data = _yaml_mapping(ROOT / "configs" / "simulator.yaml")
    parameter = data["world"]["expected_transactions_per_customer_local_day"]
    del parameter[missing_key]

    with pytest.raises(ConfigValidationError, match="expected_transactions_per_customer_local_day"):
        validate_simulator_config(data)


def test_smoke_is_five_locked_episodes_and_pilot_allocation_stays_unchanged() -> None:
    simulator = load_simulator_config(ROOT / "configs" / "simulator.yaml")
    episodes = load_episode_config(ROOT / "configs" / "episodes.yaml")
    smoke_episodes = episodes.profile("smoke")
    pilot_episodes = episodes.profile("pilot")

    assert simulator.scale_for("smoke").customer_count.value < simulator.scale_for("pilot").customer_count.value
    assert simulator.scale_for("smoke").merchant_count.value < simulator.scale_for("pilot").merchant_count.value
    assert smoke_episodes.total_episode_count < pilot_episodes.total_episode_count
    assert smoke_episodes.total_episode_count == 5
    assert pilot_episodes.total_episode_count == 60
    assert {role: item.value for role, item in smoke_episodes.role_episode_counts.items()} == {
        "development": 1,
        "calibration": 2,
        "test": 2,
    }
    assert {role: item.value for role, item in pilot_episodes.role_episode_counts.items()} == {
        "development": 12,
        "calibration": 24,
        "test": 24,
    }
    assert sum(item.value for item in smoke_episodes.clean_episode_counts.values()) == 1
    assert smoke_episodes.multi_cause_episode_count.value == 0
    assert {item.template for item in smoke_episodes.assignments} == {
        "clean",
        "data_pipeline",
        "seasonal",
    }
    assert episodes.protocol.duration_days.value == 30
    assert episodes.protocol.minimum_incidents.value == 0
    assert episodes.protocol.maximum_incidents.value == 2
    assert set(type(simulator.scale_for("smoke")).model_fields) == {"customer_count", "merchant_count"}
    assert set(type(smoke_episodes).model_fields) == {
        "role_episode_counts",
        "clean_episode_counts",
        "multi_cause_episode_count",
        "assignments",
    }


def test_locked_protocol_change_is_rejected() -> None:
    data = _yaml_mapping(ROOT / "configs" / "episodes.yaml")
    data["protocol"]["maximum_incidents"]["value"] = 3

    with pytest.raises(ConfigValidationError, match="incident range is fixed at 0 through 2"):
        validate_episode_config(data)


def test_valid_config_validation_does_not_mutate_input() -> None:
    data = _yaml_mapping(ROOT / "configs" / "simulator.yaml")
    original = deepcopy(data)

    validate_simulator_config(data)

    assert data == original
