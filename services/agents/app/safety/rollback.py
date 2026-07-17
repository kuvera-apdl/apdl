"""Explicitly unavailable automatic rollback capability.

The OSS developer preview has no canonical guardrail storage, live health
monitor, deployment-version contract, or rollback executor.  This module must
therefore never return a value that callers could interpret as evidence that
an experiment is healthy or that rollback was considered and declined.
"""

from __future__ import annotations

from typing import NoReturn


class RollbackUnavailableError(RuntimeError):
    """Raised whenever automatic rollback is requested in this release."""


def _unavailable() -> NoReturn:
    raise RollbackUnavailableError(
        "Automatic experiment health evaluation and rollback are unavailable: "
        "no canonical guardrail, deployment-version, or rollback contract exists."
    )


class ExperimentRollbackMonitor:
    """Fail closed without exposing synthetic health or threshold inputs."""

    async def evaluate(self, project_id: str, experiment_id: str) -> NoReturn:
        """Reject health evaluation rather than manufacturing a safe verdict."""
        del project_id, experiment_id
        _unavailable()

    async def execute_rollback(self, project_id: str, experiment_id: str) -> NoReturn:
        """Reject rollback because there is no version-fenced executor."""
        del project_id, experiment_id
        _unavailable()
