"""Self-hosted Managed Agents environment worker (decision D4 / Phase 3).

Runs APDL's own outbound-polling worker so that Managed Agents *tool execution*
(bash, file edits, the customer's build/test) happens inside OUR container, in
OUR VPC, rather than on Anthropic's infrastructure — while Anthropic still runs
the agent loop. This container is the untrusted-code boundary; harden it in
deploy (non-root, read-only rootfs + a writable /workspace, dropped caps, egress
allowlist, resource caps) — see ``Dockerfile.worker`` and Phase 4.

INTEGRATION-UNTESTED: uses the Managed Agents beta SDK
(``anthropic.lib.environments``) and requires ``ANTHROPIC_ENVIRONMENT_KEY`` +
``CODEGEN_ENVIRONMENT_ID``. ``anthropic`` is imported lazily; validate against
the live beta worker before relying on it.

Token custody (self-hosted): the Anthropic-side git proxy applies only to the
*cloud* sandbox. Here the orchestrator clones the repo with the installation
token and must keep it out of the agent's tool process — clone/push from the
orchestrator, and strip credentials from the in-container git config.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def _require(name: str) -> str:
    value = os.getenv(name, "")
    if not value:
        raise RuntimeError(
            f"{name} is required to run the self-hosted environment worker."
        )
    return value


async def run_worker(workdir: str = "/workspace") -> None:
    """Run the outbound-polling environment worker until terminated."""
    environment_key = _require("ANTHROPIC_ENVIRONMENT_KEY")
    environment_id = _require("CODEGEN_ENVIRONMENT_ID")

    from anthropic import AsyncAnthropic  # lazy: beta SDK, optional for tests
    from anthropic.lib.environments import EnvironmentWorker

    async with AsyncAnthropic(auth_token=environment_key) as client:
        logger.info("Starting self-hosted environment worker for %s", environment_id)
        await EnvironmentWorker(
            client,
            environment_id=environment_id,
            environment_key=environment_key,
            workdir=workdir,
        ).run()
