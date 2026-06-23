"""Kill switch for code automation.

A global env flag (``CODEGEN_KILL_SWITCH``) halts all changeset jobs; a
per-project denylist (``CODEGEN_DISABLED_PROJECTS``, comma-separated) halts
specific projects. Checked at the start of every job so a runaway can be stopped
without a deploy.
"""

from __future__ import annotations

import os

_TRUE = {"1", "true", "yes", "on"}


def automation_enabled(project_id: str) -> bool:
    """Return ``False`` when code automation is globally or per-project disabled."""
    if os.getenv("CODEGEN_KILL_SWITCH", "").strip().lower() in _TRUE:
        return False
    disabled = os.getenv("CODEGEN_DISABLED_PROJECTS", "")
    blocked = {p.strip() for p in disabled.split(",") if p.strip()}
    return project_id not in blocked
