from __future__ import annotations

import json
from dataclasses import replace

from triage.llm import FakeLLMClient, LLMClient, load_llm_config, resolve_openrouter_api_key


def _response(content: str, *, finish_reason: str = "stop", cost: float | None = None) -> dict:
    usage = {
        "prompt_tokens": 56,
        "completion_tokens": 34,
        "completion_tokens_details": {"reasoning_tokens": 0},
    }
    if cost is not None:
        usage["cost"] = cost
    return {
        "model": "qwen/qwen3.7-flash-20260727",
        "provider": "Alibaba",
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        "usage": usage,
    }


def _call(client: LLMClient):
    return client.complete_json(
        '{"sql":"SELECT 1"}',
        episode_id="ep-a",
        as_of_time="2026-02-02T00:00:00Z",
        data_snapshot="snapshot-digest",
    )


def test_fake_client_is_offline_and_records_usage_and_cache_key(monkeypatch) -> None:
    import urllib.request

    def fail_if_network(*_args, **_kwargs):
        raise AssertionError("fake client attempted network access")

    monkeypatch.setattr(urllib.request, "urlopen", fail_if_network)
    client = FakeLLMClient(response={"sql": "SELECT 1"})

    result = _call(client)

    assert result.ok
    assert result.value == {"sql": "SELECT 1"}
    assert result.record.model == "qwen/qwen3.7-flash"
    assert result.record.canonical_slug == "qwen/qwen3.7-flash-20260727"
    assert result.record.provider == "fake-provider"
    assert result.record.prompt_tokens == 8
    assert result.record.completion_tokens == 4
    assert result.record.reasoning_tokens == 0
    assert result.record.cost_usd > 0
    assert len(result.record.cache_key) == 64
    assert client.requests[0]["reasoning"] == {"enabled": False}
    assert client.requests[0]["temperature"] == client.config.temperature
    assert client.requests[0]["max_tokens"] == client.config.max_tokens
    assert client.requests[0]["seed"] == client.config.seed


def test_cache_key_changes_with_snapshot_or_as_of_context() -> None:
    client = FakeLLMClient(responses=[{"answer": 1}, {"answer": 1}, {"answer": 1}])
    first = client.complete_json(
        "prompt", episode_id="ep-a", as_of_time="2026-02-02T00:00:00Z", data_snapshot="a"
    )
    second = client.complete_json(
        "prompt", episode_id="ep-a", as_of_time="2026-02-02T00:00:00Z", data_snapshot="b"
    )
    third = client.complete_json(
        "prompt", episode_id="ep-a", as_of_time="2026-02-03T00:00:00Z", data_snapshot="a"
    )

    assert len({first.record.cache_key, second.record.cache_key, third.record.cache_key}) == 3


def test_invalid_json_empty_content_and_token_limit_return_structured_errors() -> None:
    invalid_json = _call(FakeLLMClient(response="not-json"))
    empty = _call(FakeLLMClient(response=_response("  ")))
    token_limit = _call(FakeLLMClient(response=_response('{"sql":"SELECT 1"}', finish_reason="length")))

    assert invalid_json.error and invalid_json.error.code == "invalid_json"
    assert empty.error and empty.error.code == "empty_content"
    assert token_limit.error and token_limit.error.code == "token_limit"


def test_cumulative_cost_ceiling_blocks_over_budget_results_and_later_calls() -> None:
    config = replace(load_llm_config(), max_total_cost_usd=1.0)
    client = FakeLLMClient(
        responses=[
            _response('{"ok":true}', cost=0.75),
            _response('{"ok":true}', cost=0.30),
            _response('{"ok":true}', cost=0.01),
        ],
        config=config,
    )

    first = _call(client)
    second = _call(client)
    third = _call(client)

    assert first.ok
    assert second.error and second.error.code == "cost_limit_exceeded"
    assert third.error and third.error.code == "cost_limit_exceeded"
    assert len(client.requests) == 2
    assert client.spent_cost_usd == 1.05


def test_api_key_is_not_in_records_or_logs(caplog, monkeypatch) -> None:
    secret = "openrouter-secret-do-not-print"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)

    def transport(_payload, _timeout):
        return _response('{"ok":true}')

    client = LLMClient(api_key=secret, transport=transport)
    result = _call(client)
    serialized = json.dumps(result.record.as_dict())

    assert result.ok
    assert secret not in serialized
    assert secret not in caplog.text


def test_key_resolution_prefers_environment_then_reads_only_dotenv_key(tmp_path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("OTHER=value\nOPENROUTER_API_KEY='dotenv-secret'\n", encoding="utf-8")

    assert resolve_openrouter_api_key(env={}, dotenv_path=dotenv) == "dotenv-secret"
    assert (
        resolve_openrouter_api_key(
            env={"OPENROUTER_API_KEY": "environment-secret"}, dotenv_path=dotenv
        )
        == "environment-secret"
    )


def test_retry_uses_configured_count_and_fake_transport_stays_offline() -> None:
    config = replace(load_llm_config(), retry_count=1, retry_backoff_ms=0)
    client = FakeLLMClient(
        responses=[TimeoutError(), _response('{"sql":"SELECT 1"}')],
        config=config,
    )

    result = _call(client)

    assert result.ok
    assert len(client.requests) == 2
