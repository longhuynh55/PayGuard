"""Append-only, cross-process LLM cost reservations for benchmark runs."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import fcntl
import json
import os
from pathlib import Path
import uuid
from typing import Any, Iterator


class CostLedgerError(ValueError):
    """Raised when the durable cost ledger is malformed or cannot be used."""


class CostLimitExceeded(CostLedgerError):
    """Raised before an LLM request would reserve more than the shared cap."""


def _money(value: Decimal | float | int | str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CostLedgerError("cost must be a finite non-negative USD amount") from exc
    if not amount.is_finite() or amount < 0:
        raise CostLedgerError("cost must be a finite non-negative USD amount")
    return amount


def _format_money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.000000000001")), "f")


class DurableCostLedger:
    """Keep call costs and in-flight reservations in a locked JSONL event file.

    An unsettled reservation remains charged after a process crash. This is a
    conservative recovery rule: an interrupted request may already have been
    accepted and billed by the provider.
    """

    def __init__(self, path: str | Path, *, cap_usd: Decimal | float | str) -> None:
        self.path = Path(path).expanduser().resolve()
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.cap_usd = _money(cap_usd)
        if self.cap_usd <= 0:
            raise CostLedgerError("cost cap must be positive")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        os.close(descriptor)
        os.chmod(self.path, 0o600)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _events_locked(self) -> list[dict[str, Any]]:
        try:
            raw = self.path.read_bytes()
        except OSError as exc:
            raise CostLedgerError(f"could not read cost ledger {self.path}: {exc}") from exc
        events: list[dict[str, Any]] = []
        if raw and not raw.endswith(b"\n"):
            raise CostLedgerError("cost ledger ends with an incomplete event")
        for line_number, line in enumerate(raw.splitlines(), start=1):
            try:
                event = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CostLedgerError(
                    f"cost ledger event {line_number} is not valid JSON"
                ) from exc
            if not isinstance(event, dict) or event.get("schema_version") != 1:
                raise CostLedgerError(f"cost ledger event {line_number} has an unknown schema")
            events.append(event)
        return events

    def _state_locked(self) -> tuple[Decimal, Decimal, set[str], int, int]:
        events = self._events_locked()
        recorded_cap: Decimal | None = None
        for event in events:
            kind = event.get("event")
            if kind == "reserve" and "cap_usd" not in event:
                raise CostLedgerError(
                    "cost ledger reserve event has no cap_usd; refusing to assume a budget"
                )
            if kind not in {"reserve", "blocked", "fake_call"} or "cap_usd" not in event:
                continue
            event_cap = _money(event["cap_usd"])
            if recorded_cap is None:
                recorded_cap = event_cap
            elif event_cap != recorded_cap:
                raise CostLedgerError(
                    "cost ledger contains events with different cap_usd values: "
                    f"{_format_money(recorded_cap)} and {_format_money(event_cap)}"
                )
        if recorded_cap is not None and recorded_cap != self.cap_usd:
            raise CostLedgerError(
                "configured cost cap differs from the cap recorded in the durable ledger "
                f"({_format_money(self.cap_usd)} != {_format_money(recorded_cap)} USD)"
            )
        reservations: dict[str, Decimal] = {}
        settled: set[str] = set()
        spent = Decimal(0)
        fake_call_count = 0
        blocked_count = 0
        for event in events:
            kind = event.get("event")
            if kind == "reserve":
                reservation_id = event.get("reservation_id")
                if not isinstance(reservation_id, str) or reservation_id in reservations:
                    raise CostLedgerError("cost ledger has a duplicate or invalid reservation")
                reservations[reservation_id] = _money(event.get("amount_usd", "-1"))
            elif kind == "settle":
                reservation_id = event.get("reservation_id")
                if reservation_id not in reservations or reservation_id in settled:
                    raise CostLedgerError("cost ledger settles an unknown reservation")
                settled_amount = _money(event.get("amount_usd", "-1"))
                actual_overrun = settled_amount > reservations[reservation_id]
                if "overrun" in event and event["overrun"] is not actual_overrun:
                    raise CostLedgerError("cost ledger overrun flag does not match its reservation")
                spent += settled_amount
                settled.add(reservation_id)
            elif kind == "fake_call":
                if _money(event.get("amount_usd", "-1")) != 0:
                    raise CostLedgerError("fake-provider cost events must record zero")
                fake_call_count += 1
            elif kind == "blocked":
                blocked_count += 1
            else:
                raise CostLedgerError(f"cost ledger has an unknown event type: {kind!r}")
        pending = sum(
            (amount for key, amount in reservations.items() if key not in settled),
            Decimal(0),
        )
        return spent, pending, settled, fake_call_count, blocked_count

    def _append_locked(self, event: dict[str, Any]) -> None:
        payload = (
            json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise CostLedgerError("could not append cost ledger event")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _event_base(event_type: str, **fields: Any) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "event": event_type,
            "recorded_at": datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
            **fields,
        }

    def reserve(
        self,
        amount_usd: Decimal | float | int | str,
        *,
        model_slug: str,
        policy: str | None,
    ) -> str:
        amount = _money(amount_usd)
        with self._locked():
            spent, pending, *_ = self._state_locked()
            if spent + pending + amount > self.cap_usd:
                self._append_locked(
                    self._event_base(
                        "blocked",
                        amount_usd=_format_money(amount),
                        cap_usd=_format_money(self.cap_usd),
                        reason="durable_cap",
                        model_slug=model_slug,
                        policy=policy,
                    )
                )
                if spent > self.cap_usd:
                    raise CostLimitExceeded(
                        "durable LLM cost ledger has already exceeded the benchmark cap "
                        f"({_format_money(spent)} > {_format_money(self.cap_usd)} USD)"
                    )
                raise CostLimitExceeded(
                    "durable LLM cost ledger would exceed the benchmark cap "
                    f"({_format_money(spent + pending)} + {_format_money(amount)} > "
                    f"{_format_money(self.cap_usd)} USD)"
                )
            reservation_id = uuid.uuid4().hex
            self._append_locked(
                self._event_base(
                    "reserve",
                    reservation_id=reservation_id,
                    amount_usd=_format_money(amount),
                    cap_usd=_format_money(self.cap_usd),
                    model_slug=model_slug,
                    policy=policy,
                )
            )
            return reservation_id

    def settle(
        self,
        reservation_id: str,
        amount_usd: Decimal | float | int | str,
        *,
        model_slug: str,
        policy: str | None,
        conservative: bool = False,
        conservative_reason: str | None = None,
    ) -> None:
        amount = _money(amount_usd)
        if conservative != (conservative_reason is not None):
            raise CostLedgerError(
                "conservative settlements must name a reason and measured settlements must not"
            )
        if conservative_reason is not None and not conservative_reason.strip():
            raise CostLedgerError("conservative_reason must be non-empty text")
        with self._locked():
            _, _, settled, *_ = self._state_locked()
            events = self._events_locked()
            reservations = {
                event["reservation_id"]: _money(event.get("amount_usd", "-1"))
                for event in events
                if event.get("event") == "reserve"
            }
            if reservation_id not in reservations or reservation_id in settled:
                raise CostLedgerError("cannot settle an unknown or already settled reservation")
            self._append_locked(
                self._event_base(
                    "settle",
                    reservation_id=reservation_id,
                    amount_usd=_format_money(amount),
                    model_slug=model_slug,
                    policy=policy,
                    conservative=conservative,
                    conservative_reason=conservative_reason,
                    overrun=amount > reservations[reservation_id],
                )
            )

    def record_fake_call(self, *, model_slug: str, policy: str | None) -> None:
        with self._locked():
            self._state_locked()
            self._append_locked(
                self._event_base(
                    "fake_call",
                    amount_usd="0.000000000000",
                    cap_usd=_format_money(self.cap_usd),
                    model_slug=model_slug,
                    policy=policy,
                )
            )

    def record_blocked(
        self,
        amount_usd: Decimal | float | int | str,
        *,
        model_slug: str,
        policy: str | None,
        reason: str,
    ) -> None:
        """Record a client-side cap refusal that happened before durable reserve."""
        amount = _money(amount_usd)
        with self._locked():
            self._state_locked()
            self._append_locked(
                self._event_base(
                    "blocked",
                    amount_usd=_format_money(amount),
                    cap_usd=_format_money(self.cap_usd),
                    model_slug=model_slug,
                    policy=policy,
                    reason=reason,
                )
            )

    def snapshot(self) -> dict[str, Any]:
        with self._locked():
            spent, pending, _, fake_calls, blocked = self._state_locked()
            events = self._events_locked()
        reservation_amounts = {
            event["reservation_id"]: _money(event["amount_usd"])
            for event in events
            if event.get("event") == "reserve"
        }
        overrun_amounts = [
            _money(event["amount_usd"]) - reservation_amounts[event["reservation_id"]]
            for event in events
            if event.get("event") == "settle"
            and _money(event["amount_usd"]) > reservation_amounts[event["reservation_id"]]
        ]
        reserved = pending
        committed = spent + reserved
        remaining = max(Decimal(0), self.cap_usd - committed)
        return {
            "path": str(self.path),
            "cap_usd": _format_money(self.cap_usd),
            "spent_usd": _format_money(spent),
            "reserved_usd": _format_money(reserved),
            "remaining_usd": _format_money(remaining),
            "fake_call_count": fake_calls,
            "blocked_request_count": blocked,
            "overrun": bool(overrun_amounts),
            "overrun_count": len(overrun_amounts),
            "overrun_usd": _format_money(sum(overrun_amounts, Decimal(0))),
        }

