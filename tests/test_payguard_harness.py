from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

import payguard.identity as identity
from evaluation.__main__ import main as evaluation_main
from payguard.__main__ import main as payguard_main
from payguard.harness import run_benchmark
from payguard.identity import RunIdentityError, assert_role_access, create_lock
from simulator.schema import PRIVATE_TABLES
from triage.cost_ledger import CostLimitExceeded, DurableCostLedger
from triage.llm import FakeLLMClient, LLMClient, _RequestFailure, load_llm_config


ROOT = Path(__file__).resolve().parents[1]


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
    role_ledger = json.loads((root / "seed-role-ledger.json").read_text())
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
    persisted_run_report = json.loads(
        (root / "policies/rule_agent_fake/run-report.json").read_text()
    )
    assert "truth_counts" not in persisted_run_report["evaluation"]
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
        json.loads((root / "seed-role-ledger.json").read_text())["entries"]
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
    assert len(json.loads((root / "seed-role-ledger.json").read_text())["entries"]) == previous_role_entries

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
