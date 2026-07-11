"""Bounded same-branch repair loop driven by actionable GitHub CI failures."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import asyncpg

from app.config import codegen_ci_repair_retries
from app.editor.base import Editor, EditRequest
from app.models.changeset import CIRemediationStatus
from app.safety.gates import evaluate_pre_push
from app.store import changesets as store
from app.store import connections as connections_store

logger = logging.getLogger(__name__)
TokenMinter = Callable[[int, str], Awaitable[str]]


def _repair_spec(original_spec: str, failure_summary: str, attempt: int, maximum: int) -> str:
    return (
        f"{original_spec}\n\n"
        f"GitHub CI repair attempt {attempt} of {maximum}. Diagnose and fix only "
        "the failure below on the existing pull-request branch. Preserve the "
        "original intent and add or update tests when the repository has a test "
        "framework. Do not suppress, skip, or weaken checks.\n\n"
        f"GitHub CI evidence:\n{failure_summary}"
    )


async def repair_failed_ci(
    pool: asyncpg.Pool,
    changeset_id: str,
    failure_key: str,
    failure_summary: str,
    *,
    editor: Editor,
    mint_token: TokenMinter,
) -> None:
    """Claim and execute one deduplicated repair on the existing PR branch."""
    maximum = codegen_ci_repair_retries()
    if maximum <= 0:
        await store.claim_ci_repair(
            pool,
            changeset_id,
            failure_key=failure_key,
            failure_summary=failure_summary,
            max_attempts=0,
        )
        return
    claimed = await store.claim_ci_repair(
        pool,
        changeset_id,
        failure_key=failure_key,
        failure_summary=failure_summary,
        max_attempts=maximum,
    )
    if claimed is None or claimed.ci_remediation_status is CIRemediationStatus.exhausted:
        return
    if not claimed.branch:
        await store.finish_ci_repair(
            pool,
            changeset_id,
            success=False,
            exhausted=True,
            error="Cannot repair GitHub CI: changeset has no PR branch.",
        )
        return

    try:
        connection = await connections_store.get_connection(pool, claimed.project_id)
        if connection is None:
            raise RuntimeError("Repository connection is missing.")
        token = await mint_token(connection.installation_id, connection.repo)
        policy = connection.policy if isinstance(connection.policy, dict) else {}
        result = await editor.implement(
            EditRequest(
                repo=connection.repo,
                project_scope=claimed.project_id,
                requirement_ledger=claimed.requirement_ledger,
                inspection_snapshot=claimed.inspection_snapshot,
                dependency_slice=claimed.dependency_slice,
                base_branch=claimed.base_branch or connection.default_base_branch,
                branch=claimed.branch,
                token=token,
                title=f"Repair CI: {claimed.task.title}",
                spec=_repair_spec(
                    claimed.task.spec,
                    failure_summary,
                    claimed.ci_retry_count,
                    maximum,
                ),
                constraints=claimed.task.constraints,
                test_cmd=policy.get("test_cmd"),
                gates_policy=policy.get("gates"),
                existing_branch=True,
                risk_level=str(claimed.task.context.get("risk_level") or "low").lower(),
            )
        )
        if result.requirement_ledger is None:
            result.requirement_ledger = claimed.requirement_ledger
        if result.contract_bundle is not None:
            await store.set_contract_bundle(pool, changeset_id, result.contract_bundle)
        if result.requirement_ledger is not None:
            await store.set_requirement_ledger(
                pool, changeset_id, result.requirement_ledger
            )
        if result.inspection_snapshot is not None or result.dependency_slice is not None:
            await store.set_inspection_evidence(
                pool,
                changeset_id,
                snapshot=result.inspection_snapshot,
                dependency_slice=result.dependency_slice,
            )
        if result.prompts:
            await store.set_prompts(pool, changeset_id, [*claimed.prompts, *result.prompts])
        if not result.success:
            await store.finish_ci_repair(
                pool,
                changeset_id,
                success=False,
                exhausted=claimed.ci_retry_count >= maximum,
                error=result.error or "CI repair edit failed.",
            )
            return
        gate = evaluate_pre_push(
            diff_stat=result.diff_stat,
            changed_paths=result.changed_paths,
            diff_text=result.diff_text,
            policy=policy.get("gates"),
        )
        if not gate.passed:
            await store.finish_ci_repair(
                pool,
                changeset_id,
                success=False,
                exhausted=claimed.ci_retry_count >= maximum,
                error="CI repair pre-push gate failed: " + "; ".join(gate.violations),
            )
            return
        await store.finish_ci_repair(pool, changeset_id, success=True)
        logger.info(
            "Pushed CI repair %d/%d for changeset %s; awaiting GitHub CI.",
            claimed.ci_retry_count,
            maximum,
            changeset_id,
        )
    except Exception as exc:
        logger.exception("CI repair failed for changeset %s", changeset_id)
        await store.finish_ci_repair(
            pool,
            changeset_id,
            success=False,
            exhausted=claimed.ci_retry_count >= maximum,
            error=f"CI repair failed: {exc}",
        )
