from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
import yaml

import payguard.identity as identity
import payguard.harness as harness
from evaluation.__main__ import main as evaluation_main
from payguard.__main__ import main as payguard_main
from payguard.config import load_benchmark_cost_cap
from payguard.harness import _policy_manifest, run_benchmark
from payguard.identity import (
    RunIdentityError,
    assert_role_access,
    benchmark_state_directory,
    create_lock,
    record_role_touches,
)
from simulator.schema import PRIVATE_TABLES
from triage.cost_ledger import CostLimitExceeded, DurableCostLedger
from triage.llm import FakeLLMClient, LLMClient, _RequestFailure, load_llm_config
from triage.__main__ import main as triage_main


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _shared_state_under_test_tmp_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        identity.BENCHMARK_STATE_DIR_ENV,
        str(tmp_path / "shared-benchmark-state"),
    )


def _lock_both_policies(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    # The implementation worktree is expected to be dirty during this test.
    # Isolate lock behavior while preserving the production clean-tree check.
    monkeypatch.setattr(identity, "_dirty_entries", lambda: ())
    assert payguard_main(["lock", "prompt-rule", "--out", str(root)]) == 0
    for policy in ("rule_human", "rule_agent_fake"):
        assert payguard_main(
            ["lock", "policy", "--policy", policy, "--out", str(root)]
        ) == 0


def test_two_policies_use_frozen_shared_base_and_resume_without_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "benchmark"
    _lock_both_policies(monkeypatch, root)
    built_providers: list[tuple[str, DurableCostLedger | None]] = []
    original_build_provider = harness.build_agent_provider

    def capture_provider(provider_name: str, *args, **kwargs):
        built_providers.append((provider_name, kwargs.get("cost_ledger")))
        return original_build_provider(provider_name, *args, **kwargs)

    monkeypatch.setattr(harness, "build_agent_provider", capture_provider)

    human = run_benchmark(
        profile="smoke",
        policy_name="rule_human",
        output_dir=root,
        analysis_role="test",
    )
    human_report_path = Path(human["evaluation"]["report_path"])
    human_report_before = human_report_path.read_bytes()
    fake = run_benchmark(
        profile="smoke",
        policy_name="rule_agent_fake",
        output_dir=root,
        analysis_role="test",
    )

    assert human["base_fingerprint"] == fake["base_fingerprint"]
    assert human["input_fingerprint_before"] == human["input_fingerprint_after"]
    assert fake["input_fingerprint_before"] == fake["input_fingerprint_after"]
    assert human["input_fingerprint_after"] == fake["input_fingerprint_after"]
    assert human_report_path.read_bytes() == human_report_before
    assert human_report_path != Path(fake["evaluation"]["report_path"])
    assert Path(fake["evaluation"]["report_path"]).is_file()
    assert human_report_path.is_relative_to(root / "base/private/reports")
    assert Path(fake["evaluation"]["report_path"]).is_relative_to(
        root / "base/private/reports"
    )
    assert human["evaluation"]["episode_ids"] == ["ep-0004", "ep-0005"]
    assert fake["evaluation"]["episode_ids"] == ["ep-0004", "ep-0005"]
    role_ledger_path = benchmark_state_directory(root) / "seed-role-ledger.json"
    role_ledger = json.loads(role_ledger_path.read_text())
    role_entries = list(role_ledger["entries"].values())
    assert any(
        entry["usage"] == "operational_execution"
        and set(entry["roles"]) == {"development", "calibration", "test"}
        for entry in role_entries
    )
    assert any(
        entry["usage"] == "analysis"
        and entry["roles"] == {"test": ["ep-0004", "ep-0005"]}
        for entry in role_entries
    )

    human_db = root / "policies/rule_human/operational/observations.sqlite"
    fake_db = root / "policies/rule_agent_fake/operational/observations.sqlite"
    assert human_db != fake_db
    assert not (root / "policies/rule_human/private").exists()
    assert not (root / "policies/rule_agent_fake/private").exists()
    with sqlite3.connect(human_db) as connection:
        human_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        human_slugs = connection.execute(
            "SELECT DISTINCT model_slug FROM decisions WHERE source='agent'"
        ).fetchall()
        assert not human_tables.intersection(PRIVATE_TABLES)
        assert human_slugs == [(None,)]
        assert [row[1] for row in connection.execute("PRAGMA database_list")] == ["main"]
    with sqlite3.connect(fake_db) as connection:
        fake_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        fake_slugs = connection.execute(
            "SELECT DISTINCT model_slug FROM decisions WHERE source='agent'"
        ).fetchall()
        assert not fake_tables.intersection(PRIVATE_TABLES)
        assert fake_slugs == [("fake/payguard-p4-round1",)]
        fake_costs = connection.execute(
            "SELECT DISTINCT cost_usd FROM decisions WHERE source='agent'"
        ).fetchall()
        assert fake_costs == [(0.0,)]
        assert [row[1] for row in connection.execute("PRAGMA database_list")] == ["main"]

    assert json.loads(human_report_path.read_text())["episode_ids"] == [
        "ep-0004",
        "ep-0005",
    ]
    fake_report = json.loads(Path(fake["evaluation"]["report_path"]).read_text())
    assert fake_report["selection"]["model_slug"] == "fake/payguard-p4-round1"
    assert all(ledger is not None for _, ledger in built_providers)
    assert {provider for provider, _ in built_providers} == {"unconfigured", "fake"}
    persisted_run_report = json.loads(
        (root / "policies/rule_agent_fake/run-report.json").read_text()
    )
    assert "truth_counts" not in persisted_run_report["evaluation"]
    assert persisted_run_report["cost_overrun"] is False
    assert evaluation_main(
        [
            "decisions",
            "--run",
            str(root / "policies/rule_agent_fake"),
            "--episode-role",
            "test",
        ]
    ) == 0

    previous_decisions = fake["triage"]["decision_rows_total"]
    previous_fake_calls = fake["cost_ledger"]["fake_call_count"]
    previous_role_entries = len(
        json.loads(role_ledger_path.read_text())["entries"]
    )
    resumed = run_benchmark(
        profile="smoke",
        policy_name="rule_agent_fake",
        output_dir=root,
        analysis_role="test",
    )
    assert resumed["triage"]["decision_rows_inserted"] == 0
    assert resumed["triage"]["decision_rows_total"] == previous_decisions
    assert resumed["cost_ledger"]["fake_call_count"] == previous_fake_calls
    assert len(json.loads(role_ledger_path.read_text())["entries"]) == previous_role_entries

    state_dir = benchmark_state_directory(root)
    shared_ledger = DurableCostLedger(state_dir / "cost-ledger.jsonl", cap_usd="10.00")
    reservation = shared_ledger.reserve(
        "0.01", model_slug="test/provider", policy="overrun-integration"
    )
    shared_ledger.settle(
        reservation,
        "0.02",
        model_slug="test/provider",
        policy="overrun-integration",
    )
    overrun_resume = run_benchmark(
        profile="smoke",
        policy_name="rule_agent_fake",
        output_dir=root,
        analysis_role="test",
    )
    assert overrun_resume["cost_overrun"] is True
    run_report = json.loads(
        (root / "policies/rule_agent_fake/run-report.json").read_text()
    )
    assert run_report["cost_overrun"] is True
    harness_report = json.loads((root / "harness-report.json").read_text())
    assert harness_report["policies"]["rule_agent_fake"]["cost_overrun"] is True

    with sqlite3.connect(fake_db) as connection:
        connection.execute("DROP TRIGGER decisions_no_update")
        connection.execute(
            "UPDATE decisions SET policy_version = 'mixed.policy.version' "
            "WHERE decision_id = (SELECT decision_id FROM decisions "
            "WHERE source = 'agent' ORDER BY decision_id LIMIT 1)"
        )
    with pytest.raises(RunIdentityError, match="decision rows do not match run-manifest.json"):
        run_benchmark(
            profile="smoke",
            policy_name="rule_agent_fake",
            output_dir=root,
            analysis_role="test",
        )

    manifest_path = root / "policies/rule_agent_fake/run-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["policy_version"] = "different-policy-version"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RunIdentityError, match="manifest mismatch"):
        run_benchmark(
            profile="smoke",
            policy_name="rule_agent_fake",
            output_dir=root,
            analysis_role="test",
        )


@pytest.mark.parametrize(
    ("command", "source_path", "before", "after"),
    [
        (
            "run",
            ROOT / "configs/triage.yaml",
            b"policy_version: payguard.triage.p4.round2.v1",
            b"policy_version: payguard.triage.p4.round2.v9",
        ),
        (
            "measure",
            ROOT / "triage/agent.py",
            b"offline provider choices",
            b"offline provider variants",
        ),
    ],
)
def test_triage_cli_run_and_measure_refuse_real_identity_mutations(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
    source_path: Path,
    before: bytes,
    after: bytes,
) -> None:
    root = tmp_path / "harness"
    policy_name = "rule_agent_fake"
    policy_dir = root / "policies" / policy_name
    (root / "base").mkdir(parents=True)
    policy_dir.mkdir(parents=True)
    hashes = identity.current_hashes(benchmark_root=root)
    identity._atomic_write_json(
        root / "harness-manifest.json",
        {"schema_version": 1, "profile": "smoke", "hashes": hashes},
    )
    base_fingerprint = "a" * 64
    identity._atomic_write_json(
        root / "base" / "base-manifest.json",
        {
            "schema_version": 1,
            "profile": "smoke",
            "hashes": hashes,
            "input_fingerprint": base_fingerprint,
        },
    )
    run_identity = identity.current_run_identity(policy_name, benchmark_root=root)
    run_manifest = _policy_manifest(
        policy_name,
        run_identity,
        base_fingerprint=base_fingerprint,
    )
    identity._atomic_write_json(policy_dir / "run-manifest.json", run_manifest)

    original = source_path.read_bytes()
    assert before in original
    source_path.write_bytes(original.replace(before, after, 1))
    try:
        args = [command, "--run", str(policy_dir), "--agent-provider", "fake"]
        if command == "measure":
            args.extend(("--incidents", "1", "--out", str(tmp_path / "measure.json")))
        assert triage_main(args) == 2
        assert "harness resume refused: current code/config/prompt/sampling hashes" in capsys.readouterr().err
    finally:
        source_path.write_bytes(original)


def test_benchmark_cost_cap_is_configured_from_master_plan() -> None:
    config = yaml.safe_load((ROOT / "configs/benchmark.yaml").read_text())
    assert load_benchmark_cost_cap() == 10.0
    assert config["benchmark"]["cost_cap_usd"]["value"] == 10.0
    assert config["benchmark"]["cost_cap_usd"]["provenance"]["reference"] == (
        "MASTER_PLAN.md, section 5."
    )


def test_lock_refuses_dirty_tree_and_role_access_requires_current_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "locked-run"
    original_current_hashes = identity.current_hashes
    monkeypatch.setattr(identity, "current_hashes", lambda **_kwargs: {"git_dirty": False})
    for dirty_entry in (
        ((" M", "triage/agent.py"),),
        (("??", "data/locked-run/generated.json"),),
    ):
        monkeypatch.setattr(identity, "_dirty_entries", lambda entry=dirty_entry: entry)
        with pytest.raises(RunIdentityError, match="uncommitted changes"):
            create_lock(root, "prompt-rule")

    monkeypatch.setattr(identity, "current_hashes", original_current_hashes)
    monkeypatch.setattr(identity, "_dirty_entries", lambda: ())
    create_lock(root, "prompt-rule")
    assert_role_access(
        root,
        operation="test.development",
        role_episode_ids={"development": ("ep-0001",)},
        policy_name=None,
    )

    original = identity.current_hashes(benchmark_root=root)
    changed = {**original, "prompt_hash": "0" * 64}
    monkeypatch.setattr(identity, "current_hashes", lambda **_kwargs: changed)
    with pytest.raises(RunIdentityError, match="current code/config/prompt/sampling hashes"):
        assert_role_access(
            root,
            operation="test.calibration",
            role_episode_ids={"calibration": ("ep-0002",)},
            policy_name=None,
        )


def test_cost_role_and_lock_state_is_shared_across_benchmark_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_root = tmp_path / "benchmark-one"
    second_root = tmp_path / "benchmark-two"
    state_dir = benchmark_state_directory(first_root)
    assert state_dir == (tmp_path / "shared-benchmark-state").resolve()
    assert benchmark_state_directory(second_root) == state_dir
    assert not state_dir.is_relative_to(first_root)
    assert not state_dir.is_relative_to(second_root)

    monkeypatch.setattr(identity, "_dirty_entries", lambda: ())
    prompt_rule_lock = create_lock(first_root, "prompt-rule")
    policy_lock = create_lock(first_root, "policy", policy_name="rule_agent_fake")
    assert create_lock(second_root, "prompt-rule") == prompt_rule_lock
    assert (
        create_lock(second_root, "policy", policy_name="rule_agent_fake") == policy_lock
    )
    assert prompt_rule_lock.is_relative_to(state_dir / "locks")
    assert policy_lock.is_relative_to(state_dir / "locks")
    assert assert_role_access(
        second_root,
        operation="shared-state.calibration",
        role_episode_ids={"calibration": ("ep-cal-0001",)},
        policy_name="rule_agent_fake",
    ) is None
    assert_role_access(
        second_root,
        operation="shared-state.test",
        role_episode_ids={"test": ("ep-test-0001",)},
        policy_name="rule_agent_fake",
    )

    for root in (first_root, second_root):
        record_role_touches(
            root,
            operation="shared-state.operational",
            role_episode_ids={"development": ("ep-dev-0001",)},
            policy_name="rule_agent_fake",
            usage="operational_execution",
        )
    global_role_ledger = json.loads(
        (state_dir / "seed-role-ledger.json").read_text(encoding="utf-8")
    )
    recorded_roots = {
        entry["benchmark_root"]
        for entry in global_role_ledger["entries"].values()
        if entry["operation"] == "shared-state.operational"
    }
    assert recorded_roots == {str(first_root.resolve()), str(second_root.resolve())}

    first_ledger = DurableCostLedger(state_dir / "cost-ledger.jsonl", cap_usd="10.00")
    first_ledger.reserve("9.99", model_slug="model/a", policy="benchmark-one")
    second_ledger = DurableCostLedger(
        benchmark_state_directory(second_root) / "cost-ledger.jsonl",
        cap_usd="10.00",
    )
    with pytest.raises(CostLimitExceeded, match="would exceed"):
        second_ledger.reserve("0.02", model_slug="model/b", policy="benchmark-two")
    assert first_ledger.snapshot()["path"] == second_ledger.snapshot()["path"]


def test_shared_state_directory_must_be_outside_benchmark_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "benchmark"
    monkeypatch.setenv(
        identity.BENCHMARK_STATE_DIR_ENV,
        str(root / ".payguard-benchmark"),
    )
    with pytest.raises(RunIdentityError, match="must be outside benchmark root"):
        benchmark_state_directory(root)


def test_shared_state_directory_uses_home_default_when_not_overridden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(identity.BENCHMARK_STATE_DIR_ENV, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert benchmark_state_directory() == (tmp_path / ".payguard-benchmark").resolve()


def test_cost_ledger_reserves_atomically_and_fake_calls_cost_zero(tmp_path: Path) -> None:
    ledger = DurableCostLedger(tmp_path / "cost-ledger.jsonl", cap_usd="0.01")
    ledger.reserve("0.009", model_slug="model/a", policy="policy-a")
    with pytest.raises(CostLimitExceeded, match="would exceed"):
        ledger.reserve("0.002", model_slug="model/b", policy="policy-b")
    snapshot = ledger.snapshot()
    assert snapshot["reserved_usd"] == "0.009000000000"
    assert snapshot["blocked_request_count"] == 1

    fake_ledger = DurableCostLedger(tmp_path / "fake-ledger.jsonl", cap_usd="10")
    fake = FakeLLMClient(
        response={"ok": True},
        cost_ledger=fake_ledger,
        ledger_policy="rule_agent_fake",
    )
    result = fake.complete_json(
        "Return JSON.",
        episode_id="ep-0001",
        as_of_time="2026-02-01T16:59:59.999999Z",
        data_snapshot="snapshot",
    )
    assert result.ok
    assert fake_ledger.snapshot()["spent_usd"] == "0.000000000000"
    assert fake_ledger.snapshot()["fake_call_count"] == 1


def test_cost_ledger_limit_survives_new_process(tmp_path: Path) -> None:
    ledger_path = tmp_path / "cross-process-ledger.jsonl"
    config = replace(load_llm_config(), max_total_cost_usd=1.0, retry_count=0)

    def transport(payload: dict[str, object], _timeout: float) -> dict[str, object]:
        return {
            "model": str(payload["model"]),
            "provider": "test-provider",
            "choices": [{"message": {"content": '{"ok":true}'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "cost": 0.00004},
        }

    client = LLMClient(
        config=config,
        transport=transport,
        cost_ledger=DurableCostLedger(ledger_path, cap_usd="0.0001"),
    )
    first = client.complete_json(
        "Return a JSON object.",
        episode_id="ep-0001",
        as_of_time="2026-02-01T16:59:59.999999Z",
        data_snapshot="snapshot-1",
    )
    assert first.ok

    child_code = """
from dataclasses import replace
import sys
from triage.cost_ledger import DurableCostLedger
from triage.llm import LLMClient, load_llm_config

def transport(payload, timeout):
    return {
        'model': payload['model'],
        'provider': 'test-provider',
        'choices': [{'message': {'content': '{\"ok\":true}'}, 'finish_reason': 'stop'}],
        'usage': {'prompt_tokens': 10, 'completion_tokens': 10, 'cost': 0.00004},
    }

ledger = DurableCostLedger(sys.argv[1], cap_usd='0.0001')
client = LLMClient(
    config=replace(load_llm_config(), max_total_cost_usd=1.0, retry_count=0),
    transport=transport,
    cost_ledger=ledger,
)
result = client.complete_json(
    'Return a JSON object.', episode_id='ep-0002',
    as_of_time='2026-02-01T16:59:59.999999Z', data_snapshot='snapshot-2',
)
print(result.error.code if result.error else 'called')
print(ledger.snapshot()['spent_usd'])
"""
    child = subprocess.run(
        [sys.executable, "-c", child_code, str(ledger_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert child.returncode == 0, child.stderr
    assert child.stdout.splitlines()[0] == "cost_limit_exceeded"
    assert child.stdout.splitlines()[1] == "0.000040000000"


def test_cost_ledger_accounts_for_each_retry_attempt(tmp_path: Path) -> None:
    attempts = 0
    config = replace(load_llm_config(), max_total_cost_usd=1.0, retry_count=1, retry_backoff_ms=0)
    ledger = DurableCostLedger(tmp_path / "retry-ledger.jsonl", cap_usd="1.0")

    def retrying_transport(payload: dict[str, object], _timeout: float) -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise _RequestFailure("timeout", "request timed out", retryable=True)
        return {
            "model": str(payload["model"]),
            "provider": "test-provider",
            "choices": [{"message": {"content": '{"ok":true}'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "cost": 0.00004},
        }

    client = LLMClient(config=config, transport=retrying_transport, cost_ledger=ledger)
    result = client.complete_json(
        "Retry accounting.",
        episode_id="ep-0001",
        as_of_time="2026-02-01T16:59:59.999999Z",
        data_snapshot="retry-snapshot",
    )
    events = [json.loads(line) for line in ledger.path.read_text().splitlines()]
    assert result.ok
    assert attempts == 2
    assert sum(event["event"] == "settle" for event in events) == 2
    assert float(ledger.snapshot()["spent_usd"]) > 0.00004


def test_cost_ledger_marks_reported_usage_over_reserved_amount(tmp_path: Path) -> None:
    config = replace(load_llm_config(), max_total_cost_usd=1.0, retry_count=0)
    ledger = DurableCostLedger(tmp_path / "overrun-ledger.jsonl", cap_usd="0.01")

    def overshoot_transport(payload: dict[str, object], _timeout: float) -> dict[str, object]:
        return {
            "model": str(payload["model"]),
            "provider": "test-provider",
            "choices": [{"message": {"content": '{"ok":true}'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "cost": 0.5},
        }

    result = LLMClient(
        config=config,
        transport=overshoot_transport,
        cost_ledger=ledger,
        ledger_policy="overshoot-test",
    ).complete_json(
        "Bounded reservation test.",
        episode_id="ep-0001",
        as_of_time="2026-02-01T16:59:59.999999Z",
        data_snapshot="overshoot-snapshot",
    )
    events = [json.loads(line) for line in ledger.path.read_text().splitlines()]
    snapshot = ledger.snapshot()
    assert result.ok
    settle = next(event for event in events if event["event"] == "settle")
    assert settle["overrun"] is True
    assert snapshot["overrun"] is True
    assert snapshot["overrun_count"] == 1
    assert float(snapshot["overrun_usd"]) > 0.49
    with pytest.raises(CostLimitExceeded, match="already exceeded"):
        ledger.reserve("0.000001", model_slug="model/a", policy="overshoot-test")


def test_cost_ledger_multi_process_race_never_exceeds_cap(tmp_path: Path) -> None:
    ledger_path = tmp_path / "racing-ledger.jsonl"
    worker = """
import sys
from triage.cost_ledger import CostLimitExceeded, DurableCostLedger

ledger = DurableCostLedger(sys.argv[1], cap_usd='1.00')
accepted = blocked = 0
for _ in range(40):
    try:
        reservation = ledger.reserve('0.01', model_slug='race/model', policy='race')
    except CostLimitExceeded:
        blocked += 1
        continue
    ledger.settle(reservation, '0.01', model_slug='race/model', policy='race')
    accepted += 1
print(f'{accepted},{blocked}')
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", worker, str(ledger_path)],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(4)
    ]
    totals = [0, 0]
    for process in processes:
        stdout, stderr = process.communicate(timeout=60)
        assert process.returncode == 0, stderr
        accepted, blocked = (int(value) for value in stdout.strip().split(","))
        totals[0] += accepted
        totals[1] += blocked

    snapshot = DurableCostLedger(ledger_path, cap_usd="1.00").snapshot()
    events = [json.loads(line) for line in ledger_path.read_text().splitlines()]
    assert totals == [100, 60]
    assert snapshot["spent_usd"] == "1.000000000000"
    assert snapshot["overrun"] is False
    assert sum(event["event"] == "reserve" for event in events) == 100
    assert sum(event["event"] == "blocked" for event in events) == 60

    reservations: dict[str, Decimal] = {}
    settled: set[str] = set()
    spent = pending = peak_committed = Decimal(0)
    for event in events:
        if event["event"] == "reserve":
            amount = Decimal(event["amount_usd"])
            reservations[event["reservation_id"]] = amount
            pending += amount
        elif event["event"] == "settle":
            reservation_id = event["reservation_id"]
            pending -= reservations[reservation_id]
            spent += Decimal(event["amount_usd"])
            settled.add(reservation_id)
        peak_committed = max(peak_committed, spent + pending)
    assert len(settled) == 100
    assert peak_committed <= Decimal("1.00")
