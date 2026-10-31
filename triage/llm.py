"""OpenRouter chat-completions client with structured, JSON-only results."""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import yaml

from simulator.storage import utc_timestamp
from triage.cost_ledger import CostLimitExceeded, DurableCostLedger


OPENROUTER_CHAT_COMPLETIONS_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs" / "llm.yaml"


class LLMConfigError(ValueError):
    """Raised when the checked-in LLM configuration is invalid."""


@dataclass(frozen=True)
class LLMConfig:
    schema_version: int
    model_id: str
    canonical_slug: str
    temperature: float
    max_tokens: int
    seed: int
    reasoning_enabled: bool
    timeout_seconds: float
    retry_count: int
    retry_backoff_ms: int
    max_total_cost_usd: float
    input_usd_per_million: float
    output_usd_per_million: float
    max_result_rows: int
    max_result_bytes: int
    max_sqlite_value_bytes: int
    sql_timeout_seconds: float

    @property
    def sampling_config(self) -> dict[str, Any]:
        return {
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "seed": self.seed,
            "reasoning": {"enabled": self.reasoning_enabled},
        }


@dataclass(frozen=True)
class LLMError:
    code: str
    message: str
    retryable: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "retryable": self.retryable}


@dataclass(frozen=True)
class LLMCallRecord:
    model: str | None
    canonical_slug: str
    provider: str | None
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    cost_usd: float
    latency_ms: float
    cache_key: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "canonical_slug": self.canonical_slug,
            "provider": self.provider,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cost_usd": self.cost_usd,
            "latency_ms": self.latency_ms,
            "cache_key": self.cache_key,
        }


@dataclass(frozen=True)
class LLMResult:
    record: LLMCallRecord
    value: Any = None
    content: str | None = None
    error: LLMError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class _RequestFailure(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


def load_llm_config(path: str | Path = DEFAULT_CONFIG_PATH) -> LLMConfig:
    """Load the single source of truth for the LLM request and tool limits."""
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("root must be a mapping")
        model = raw["model"]
        sampling = raw["sampling"]
        reasoning = sampling["reasoning"]
        request = raw["request"]
        budget = raw["budget"]
        pricing = raw["pricing"]
        tool = raw["tool"]
        config = LLMConfig(
            schema_version=int(raw["schema_version"]),
            model_id=str(model["id"]),
            canonical_slug=str(model["canonical_slug"]),
            temperature=float(sampling["temperature"]),
            max_tokens=int(sampling["max_tokens"]),
            seed=int(sampling["seed"]),
            reasoning_enabled=bool(reasoning["enabled"]),
            timeout_seconds=float(request["timeout_seconds"]),
            retry_count=int(request["retry_count"]),
            retry_backoff_ms=int(request["retry_backoff_ms"]),
            max_total_cost_usd=float(budget["max_total_cost_usd"]),
            input_usd_per_million=float(pricing["input_usd_per_million"]),
            output_usd_per_million=float(pricing["output_usd_per_million"]),
            max_result_rows=int(tool["max_result_rows"]),
            max_result_bytes=int(tool["max_result_bytes"]),
            max_sqlite_value_bytes=int(tool["max_sqlite_value_bytes"]),
            sql_timeout_seconds=float(tool["sql_timeout_seconds"]),
        )
    except (OSError, KeyError, TypeError, ValueError, yaml.YAMLError) as exc:
        raise LLMConfigError("configs/llm.yaml is missing or invalid") from exc

    if (
        config.schema_version < 1
        or not config.model_id
        or not config.canonical_slug
        or not 0.0 <= config.temperature <= 2.0
        or config.max_tokens < 1
        or config.retry_count < 0
        or config.retry_backoff_ms < 0
        or config.timeout_seconds <= 0
        or config.max_total_cost_usd <= 0
        or config.input_usd_per_million < 0
        or config.output_usd_per_million < 0
        or config.max_result_rows < 1
        or config.max_result_bytes < 1
        or config.max_sqlite_value_bytes < 1
        or config.sql_timeout_seconds <= 0
    ):
        raise LLMConfigError("configs/llm.yaml contains out-of-range values")
    return config


def _read_dotenv_key(path: Path) -> str | None:
    """Read just the OpenRouter key without exporting or logging other values."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        if name.strip() != "OPENROUTER_API_KEY":
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return value or None
    return None


def resolve_openrouter_api_key(
    *, env: Mapping[str, str] | None = None, dotenv_path: str | Path | None = None
) -> str | None:
    """Resolve the key from the environment first, then the repository root .env."""
    environment = os.environ if env is None else env
    value = environment.get("OPENROUTER_API_KEY", "").strip()
    if value:
        return value
    root_env = Path(dotenv_path) if dotenv_path is not None else DEFAULT_CONFIG_PATH.parents[1] / ".env"
    return _read_dotenv_key(root_env)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_without_nonstandard_constants(content: str) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-standard JSON constant: {value}")

    return json.loads(content, parse_constant=reject_constant)


class LLMClient:
    """OpenRouter-compatible JSON client; pass ``transport`` for offline use."""

    def __init__(
        self,
        *,
        config: LLMConfig | None = None,
        config_path: str | Path = DEFAULT_CONFIG_PATH,
        api_key: str | None = None,
        dotenv_path: str | Path | None = None,
        transport: Callable[[dict[str, Any], float], Any] | None = None,
        cost_ledger: DurableCostLedger | None = None,
        ledger_policy: str | None = None,
    ) -> None:
        self.config = config or load_llm_config(config_path)
        self._api_key = api_key
        self._dotenv_path = Path(dotenv_path) if dotenv_path is not None else None
        self._transport = transport
        self._spent_cost_usd = 0.0
        self._cost_ledger = cost_ledger
        self._ledger_policy = ledger_policy

    @property
    def spent_cost_usd(self) -> float:
        return self._spent_cost_usd

    def complete_json(
        self,
        prompt: str,
        *,
        episode_id: str,
        as_of_time: str,
        data_snapshot: str,
    ) -> LLMResult:
        """Call the configured model once logically and always return a result record."""
        started = time.monotonic()
        normalized_as_of = as_of_time
        try:
            normalized_as_of = utc_timestamp(as_of_time)
        except (TypeError, ValueError):
            pass
        cache_key = self._cache_key(
            prompt,
            episode_id=episode_id,
            as_of_time=normalized_as_of,
            data_snapshot=data_snapshot,
        )

        payload = {
            "model": self.config.model_id,
            "messages": [{"role": "user", "content": prompt}],
            **self.config.sampling_config,
            "response_format": {"type": "json_object"},
        }
        reserved_amount = self._maximum_request_cost(payload)
        if self._spent_cost_usd >= self.config.max_total_cost_usd:
            if self._cost_ledger is not None:
                self._cost_ledger.record_blocked(
                    reserved_amount,
                    model_slug=self.config.canonical_slug,
                    policy=self._ledger_policy,
                    reason="in_memory_cap",
                )
            record = self._empty_record(cache_key, started)
            return LLMResult(
                record,
                error=LLMError("cost_limit_exceeded", "LLM cost ceiling has been reached."),
            )

        if self._transport is None and not self._api_key:
            self._api_key = resolve_openrouter_api_key(dotenv_path=self._dotenv_path)
        if self._transport is None and not self._api_key:
            record = self._empty_record(cache_key, started)
            return LLMResult(record, error=LLMError("missing_api_key", "OpenRouter API key is unavailable."))

        response: Any = None
        response_record: LLMCallRecord | None = None
        request_failure: _RequestFailure | None = None
        for attempt in range(self.config.retry_count + 1):
            reservation_id: str | None = None
            reserved_amount = self._maximum_request_cost(payload)
            if self._cost_ledger is not None:
                if getattr(self, "_fake_cost_free", False):
                    self._cost_ledger.record_fake_call(
                        model_slug=self.config.canonical_slug,
                        policy=self._ledger_policy,
                    )
                else:
                    try:
                        reservation_id = self._cost_ledger.reserve(
                            reserved_amount,
                            model_slug=self.config.canonical_slug,
                            policy=self._ledger_policy,
                        )
                    except CostLimitExceeded as exc:
                        record = self._empty_record(cache_key, started)
                        return LLMResult(
                            record,
                            error=LLMError("cost_limit_exceeded", str(exc)),
                        )
            try:
                response = self._send(payload)
                if not getattr(self, "_fake_cost_free", False):
                    if isinstance(response, Mapping):
                        response_record = self._record_from_response(
                            response, cache_key, started
                        )
                        usage = response.get("usage")
                        has_metered_usage = isinstance(usage, Mapping) and any(
                            key in usage
                            for key in (
                                "prompt_tokens",
                                "completion_tokens",
                                "cost",
                            )
                        )
                        settled_amount = (
                            response_record.cost_usd if has_metered_usage else reserved_amount
                        )
                        conservative = not has_metered_usage
                    else:
                        settled_amount = reserved_amount
                        conservative = True
                    if reservation_id is not None:
                        self._cost_ledger.settle(
                            reservation_id,
                            settled_amount,
                            model_slug=self.config.canonical_slug,
                            policy=self._ledger_policy,
                            conservative=conservative,
                        )
                request_failure = None
                break
            except _RequestFailure as exc:
                if reservation_id is not None:
                    self._cost_ledger.settle(
                        reservation_id,
                        reserved_amount,
                        model_slug=self.config.canonical_slug,
                        policy=self._ledger_policy,
                        conservative=True,
                    )
                request_failure = exc
                if not exc.retryable or attempt >= self.config.retry_count:
                    break
                if self.config.retry_backoff_ms:
                    time.sleep(self.config.retry_backoff_ms / 1000.0)
            except Exception:
                if reservation_id is not None:
                    self._cost_ledger.settle(
                        reservation_id,
                        reserved_amount,
                        model_slug=self.config.canonical_slug,
                        policy=self._ledger_policy,
                        conservative=True,
                    )
                request_failure = _RequestFailure("transport_error", "OpenRouter request failed.")
                break

        if request_failure is not None:
            record = self._empty_record(cache_key, started)
            return LLMResult(
                record,
                error=LLMError(
                    request_failure.code,
                    request_failure.message,
                    retryable=request_failure.retryable,
                ),
            )

        if not isinstance(response, Mapping):
            record = self._empty_record(cache_key, started)
            return LLMResult(record, error=LLMError("invalid_response", "OpenRouter returned an invalid response."))

        model = _optional_text(response.get("model"))
        provider = _optional_text(response.get("provider"))
        if response.get("error") is not None:
            record = self._record_from_response(response, cache_key, started)
            self._spent_cost_usd += record.cost_usd
            if self._spent_cost_usd > self.config.max_total_cost_usd:
                return LLMResult(
                    record,
                    error=LLMError("cost_limit_exceeded", "LLM cost ceiling was exceeded by this call."),
                )
            return LLMResult(record, error=LLMError("provider_error", "OpenRouter rejected the request."))
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            record = self._record_from_response(response, cache_key, started)
            self._spent_cost_usd += record.cost_usd
            if self._spent_cost_usd > self.config.max_total_cost_usd:
                return LLMResult(
                    record,
                    error=LLMError("cost_limit_exceeded", "LLM cost ceiling was exceeded by this call."),
                )
            return LLMResult(record, error=LLMError("invalid_response", "OpenRouter returned no chat completion."))

        choice = choices[0]
        finish_reason = str(choice.get("finish_reason") or "")
        message = choice.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        if not provider:
            provider = _optional_text(choice.get("provider"))
        record = response_record or self._record_from_response(response, cache_key, started)
        if provider != record.provider:
            record = replace(record, provider=provider)
        self._spent_cost_usd += record.cost_usd

        if self._spent_cost_usd > self.config.max_total_cost_usd:
            return LLMResult(
                record,
                error=LLMError("cost_limit_exceeded", "LLM cost ceiling was exceeded by this call."),
            )
        if finish_reason in {"length", "max_tokens"}:
            return LLMResult(record, error=LLMError("token_limit", "LLM completion reached its token limit."))
        if not isinstance(content, str) or not content.strip():
            return LLMResult(record, error=LLMError("empty_content", "LLM returned empty content."))
        try:
            value = _json_without_nonstandard_constants(content)
        except (json.JSONDecodeError, ValueError):
            return LLMResult(record, error=LLMError("invalid_json", "LLM content is not valid JSON."))
        return LLMResult(record, value=value, content=content)

    def _maximum_request_cost(self, payload: Mapping[str, Any]) -> Decimal:
        """Reserve against the UTF-8 byte upper bound for prompt tokens."""
        encoded_bytes = len(_canonical_json(payload).encode("utf-8")) + 16
        input_rate = Decimal(str(self.config.input_usd_per_million))
        output_rate = Decimal(str(self.config.output_usd_per_million))
        input_cost = Decimal(encoded_bytes) * input_rate / Decimal(1_000_000)
        output_cost = Decimal(self.config.max_tokens) * output_rate / Decimal(1_000_000)
        return input_cost + output_cost

    def _cache_key(
        self, prompt: str, *, episode_id: str, as_of_time: str, data_snapshot: str
    ) -> str:
        cache_payload = {
            "prompt": prompt,
            "model": self.config.model_id,
            "canonical_slug": self.config.canonical_slug,
            "sampling": self.config.sampling_config,
            "schema_version": self.config.schema_version,
            "episode_id": episode_id,
            "as_of_time": as_of_time,
            "data_snapshot": data_snapshot,
        }
        return hashlib.sha256(_canonical_json(cache_payload).encode("utf-8")).hexdigest()

    def _empty_record(self, cache_key: str, started: float) -> LLMCallRecord:
        return LLMCallRecord(
            model=None,
            canonical_slug=self.config.canonical_slug,
            provider=None,
            prompt_tokens=0,
            completion_tokens=0,
            reasoning_tokens=0,
            cost_usd=0.0,
            latency_ms=(time.monotonic() - started) * 1000,
            cache_key=cache_key,
        )

    def _record_from_response(self, response: Mapping[str, Any], cache_key: str, started: float) -> LLMCallRecord:
        usage = response.get("usage")
        usage = usage if isinstance(usage, Mapping) else {}
        details = usage.get("completion_tokens_details")
        details = details if isinstance(details, Mapping) else {}
        prompt_tokens = _nonnegative_int(usage.get("prompt_tokens"))
        completion_tokens = _nonnegative_int(usage.get("completion_tokens"))
        reasoning_tokens = _nonnegative_int(
            details.get("reasoning_tokens", usage.get("reasoning_tokens"))
        )
        raw_cost = usage.get("cost")
        cost_usd = _finite_nonnegative_float(raw_cost)
        if cost_usd is None:
            cost_usd = (
                prompt_tokens * self.config.input_usd_per_million
                + completion_tokens * self.config.output_usd_per_million
            ) / 1_000_000
        if getattr(self, "_fake_cost_free", False) and self._cost_ledger is not None:
            cost_usd = 0.0
        return LLMCallRecord(
            model=_optional_text(response.get("model")),
            canonical_slug=self.config.canonical_slug,
            provider=_optional_text(response.get("provider")),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            reasoning_tokens=reasoning_tokens,
            cost_usd=cost_usd,
            latency_ms=(time.monotonic() - started) * 1000,
            cache_key=cache_key,
        )

    def _send(self, payload: dict[str, Any]) -> Any:
        if self._transport is not None:
            return self._transport(payload, self.config.timeout_seconds)
        assert self._api_key is not None
        request = urllib.request.Request(
            OPENROUTER_CHAT_COMPLETIONS_URL,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            retryable = status in {408, 429} or status >= 500
            code = "http_error" if status < 500 else "provider_unavailable"
            try:
                error_payload = json.loads(exc.read().decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                error_payload = None
            finally:
                exc.close()
            if isinstance(error_payload, Mapping):
                usage = error_payload.get("usage")
                has_usage = isinstance(usage, Mapping) and any(
                    key in usage for key in ("prompt_tokens", "completion_tokens", "cost")
                )
                if has_usage:
                    preserved = dict(error_payload)
                    preserved.setdefault("error", {"message": f"HTTP {status}"})
                    return preserved
            raise _RequestFailure(code, f"OpenRouter returned HTTP {status}.", retryable=retryable) from None
        except (TimeoutError, urllib.error.URLError) as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError):
                raise _RequestFailure("timeout", "OpenRouter request timed out.", retryable=True) from None
            raise _RequestFailure("network_error", "OpenRouter request failed.", retryable=True) from None
        except OSError:
            raise _RequestFailure("network_error", "OpenRouter request failed.", retryable=True) from None

        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise _RequestFailure("invalid_response", "OpenRouter returned invalid JSON.") from None
        return parsed


class FakeLLMClient(LLMClient):
    """Deterministic offline client for tests; it never opens a network connection."""

    def __init__(
        self,
        responses: Sequence[Any] | None = None,
        *,
        response: Any = None,
        config: LLMConfig | None = None,
        config_path: str | Path = DEFAULT_CONFIG_PATH,
        response_factory: Callable[[dict[str, Any]], Any] | None = None,
        cost_ledger: DurableCostLedger | None = None,
        ledger_policy: str | None = None,
    ) -> None:
        if responses is not None and response is not None:
            raise ValueError("provide either responses or response, not both")
        if response_factory is not None and (responses is not None or response is not None):
            raise ValueError("provide either fake responses or response_factory, not both")
        initial = responses if responses is not None else [response if response is not None else {"sql": "SELECT 1"}]
        self._fake_responses = deque(initial)
        self._fake_response_factory = response_factory
        self._fake_cost_free = True
        self.requests: list[dict[str, Any]] = []
        super().__init__(
            config=config,
            config_path=config_path,
            transport=self._fake_send,
            cost_ledger=cost_ledger,
            ledger_policy=ledger_policy,
        )

    def _fake_send(self, payload: dict[str, Any], _timeout_seconds: float) -> Any:
        self.requests.append(payload)
        if self._fake_response_factory is not None:
            item = self._fake_response_factory(payload)
        else:
            if not self._fake_responses:
                raise _RequestFailure("fake_response_exhausted", "Fake LLM has no remaining response.")
            item = self._fake_responses.popleft()
        if isinstance(item, BaseException):
            if isinstance(item, TimeoutError):
                raise _RequestFailure("timeout", "OpenRouter request timed out.", retryable=True)
            raise _RequestFailure("transport_error", "OpenRouter request failed.")
        if isinstance(item, Mapping) and "choices" in item:
            return item
        content = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        return {
            "model": self.config.model_id,
            "provider": "fake-provider",
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 8,
                "completion_tokens": 4,
                "completion_tokens_details": {"reasoning_tokens": 0},
            },
        }


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _nonnegative_int(value: Any) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, result)


def _finite_nonnegative_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if result < 0 or result == float("inf") or result != result:
        return None
    return result
