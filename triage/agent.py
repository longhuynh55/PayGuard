"""Agent proposal interface used by the P4 decision loop."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol


class AgentProposalProvider(Protocol):
    """Provide a cause/action proposal for an incident not handled by a rule."""

    def propose(
        self,
        incident: Mapping[str, Any],
        *,
        as_of_time: str,
        checklist_results: Mapping[str, Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        """Return proposal fields consumed by the code-owned gate."""


class UnconfiguredAgent:
    """Safe round-one fallback until the P4.4b agent is connected."""

    def propose(
        self,
        incident: Mapping[str, Any],
        *,
        as_of_time: str,
        checklist_results: Mapping[str, Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        del incident, as_of_time
        return {
            "cause": "other",
            "runbook": None,
            "action": "human_review",
            "checklist_results": checklist_results,
            "reason": "agent_not_configured",
        }
