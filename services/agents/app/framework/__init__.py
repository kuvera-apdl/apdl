"""Agent framework — Template Method base class, registry, and gating.

See :mod:`app.framework.base` for the lifecycle and ``docs/agent-framework.md``
for the authoring guide.
"""

from __future__ import annotations

from app.framework.base import BaseAgent
from app.framework.context import AgentContext, AgentResult, MemoryEntry
from app.framework.custom import RESERVED_STATE_KEYS, CustomAgent, validate_definition
from app.framework.gating import GateDecision, gate_action
from app.framework.registry import (
    get_agent,
    is_registered,
    list_agents,
    register_agent,
    registered_agents,
)

__all__ = [
    "BaseAgent",
    "AgentContext",
    "AgentResult",
    "CustomAgent",
    "MemoryEntry",
    "RESERVED_STATE_KEYS",
    "validate_definition",
    "GateDecision",
    "gate_action",
    "register_agent",
    "get_agent",
    "is_registered",
    "list_agents",
    "registered_agents",
]
