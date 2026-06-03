"""Agent graph package.

Importing this package registers every built-in agent with the framework
registry as an import side-effect, so the supervisor can resolve agents by
name without importing each class. New agents generated via
``scripts/new_agent.py`` are appended below automatically.
"""

from __future__ import annotations

from app.graphs import (  # noqa: F401  (imported for registration side-effects)
    behavior_analysis,
    experiment_design,
    feature_proposal,
    personalization,
)
