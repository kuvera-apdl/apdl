"""Agent registry — decouples the supervisor from concrete agent classes.

Agents self-register with the ``@register_agent`` decorator. The supervisor
(and any other caller) discovers them by name instead of importing each class
and hard-coding an if-chain. Importing :mod:`app.graphs` triggers registration
of all built-in agents as an import side-effect.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.framework.base import BaseAgent

_REGISTRY: dict[str, type["BaseAgent"]] = {}


def register_agent(cls: type["BaseAgent"]) -> type["BaseAgent"]:
    """Class decorator that registers an agent under its ``name`` attribute."""
    name = getattr(cls, "name", None)
    if not name:
        raise ValueError(f"{cls.__name__} must define a non-empty 'name' to register")
    if name in _REGISTRY and _REGISTRY[name] is not cls:
        raise ValueError(f"Duplicate agent name '{name}' ({cls.__name__})")
    _REGISTRY[name] = cls
    return cls


def get_agent(name: str) -> "BaseAgent":
    """Instantiate the agent registered under ``name``.

    Raises:
        KeyError: If no agent is registered under that name.
    """
    if name not in _REGISTRY:
        raise KeyError(f"No agent registered as '{name}'. Known: {sorted(_REGISTRY)}")
    return _REGISTRY[name]()


def is_registered(name: str) -> bool:
    return name in _REGISTRY


def list_agents() -> list[str]:
    """Return registered agent names, ordered by their declared pipeline order."""
    return [name for name, _ in sorted(_REGISTRY.items(), key=lambda kv: kv[1].order)]


def registered_agents() -> dict[str, type["BaseAgent"]]:
    """Return a copy of the full registry mapping name -> class."""
    return dict(_REGISTRY)
