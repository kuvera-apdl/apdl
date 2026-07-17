"""PostgreSQL authority for LLM policy, cost reservations, and audit ledgers."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Literal, NoReturn, cast
from uuid import UUID, uuid4

from app.llm.contracts import (
    ErrorClassification,
    LlmBudgetExceededError,
    LlmCostOverrunError,
    LlmGovernanceError,
    LlmGovernanceUnavailableError,
    LlmPolicyDeniedError,
    LlmRequestContext,
    LlmRunInactiveError,
    PreparedLlmAttempt,
    ProjectLlmPolicy,
    ProviderName,
    ProviderPolicy,
    cost_usd_micros,
)


_TERMINAL_ATTEMPT_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
LLM_RECONCILIATION_INTERVAL_SECONDS = 30.0

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LlmReconciliationResult:
    prepared_blocked: int
    in_flight_cancelled: int
    calls_cancelled: int


async def reconcile_orphaned_llm_attempts(pool: Any) -> LlmReconciliationResult:
    """Terminalize reservations whose owning execution has no live lease.

    A prepared attempt provably never crossed provider egress, so its
    reservation is released with zero charge. An in-flight attempt may have
    reached a provider; it is conservatively charged at the full reservation.
    The logical call is then made terminal so operators can distinguish crash
    recovery from a provider result and budgets never retain an active row
    forever.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                "apdl:llm-attempt-reconciliation",
            )
            reconciled = await conn.fetch(
                """
                WITH orphaned AS (
                    SELECT attempt.attempt_id, attempt.call_id,
                           attempt.status AS previous_status
                    FROM llm_provider_attempts AS attempt
                    JOIN llm_calls AS call ON call.call_id = attempt.call_id
                    WHERE attempt.status IN ('prepared', 'in_flight')
                      AND (
                          (
                              call.execution_kind = 'agent_run'
                              AND NOT EXISTS (
                                  SELECT 1
                                  FROM agent_runs AS run
                                  WHERE run.run_id = call.run_id
                                    AND run.project_id = call.project_id
                                    AND run.status IN ('started', 'running')
                                    AND run.lease_owner_id = call.execution_owner_id
                                    AND run.lease_expires_at > now()
                              )
                          )
                          OR (
                              call.execution_kind = 'custom_agent_test'
                              AND NOT EXISTS (
                                  SELECT 1
                                  FROM custom_agent_test_runs AS test_run
                                  WHERE test_run.test_run_id = call.run_id
                                    AND test_run.project_id = call.project_id
                                    AND test_run.status = 'running'
                                    AND test_run.lease_expires_at > now()
                              )
                          )
                      )
                    FOR UPDATE OF attempt SKIP LOCKED
                )
                UPDATE llm_provider_attempts AS attempt
                SET status = CASE
                        WHEN orphaned.previous_status = 'prepared'
                            THEN 'blocked'
                        ELSE 'cancelled'
                    END,
                    charged_cost_usd_micros = CASE
                        WHEN orphaned.previous_status = 'prepared'
                            THEN 0
                        ELSE attempt.reserved_cost_usd_micros
                    END,
                    retryable = FALSE,
                    error_classification = 'cancelled',
                    error_message = CASE
                        WHEN orphaned.previous_status = 'prepared'
                            THEN 'Owning execution ended before provider egress'
                        ELSE 'Owning execution ended with provider outcome unknown'
                    END,
                    completed_at = now()
                FROM orphaned
                WHERE attempt.attempt_id = orphaned.attempt_id
                RETURNING orphaned.call_id, orphaned.previous_status
                """
            )
            calls_cancelled = int(
                await conn.fetchval(
                    """
                    WITH orphaned_calls AS (
                        SELECT call.call_id
                        FROM llm_calls AS call
                        WHERE call.status IN ('prepared', 'in_flight')
                          AND (
                              (
                                  call.execution_kind = 'agent_run'
                                  AND NOT EXISTS (
                                      SELECT 1
                                      FROM agent_runs AS run
                                      WHERE run.run_id = call.run_id
                                        AND run.project_id = call.project_id
                                        AND run.status IN ('started', 'running')
                                        AND run.lease_owner_id =
                                            call.execution_owner_id
                                        AND run.lease_expires_at > now()
                                  )
                              )
                              OR (
                                  call.execution_kind = 'custom_agent_test'
                                  AND NOT EXISTS (
                                      SELECT 1
                                      FROM custom_agent_test_runs AS test_run
                                      WHERE test_run.test_run_id = call.run_id
                                        AND test_run.project_id = call.project_id
                                        AND test_run.status = 'running'
                                        AND test_run.lease_expires_at > now()
                                  )
                              )
                          )
                          AND NOT EXISTS (
                              SELECT 1
                              FROM llm_provider_attempts AS active_attempt
                              WHERE active_attempt.call_id = call.call_id
                                AND active_attempt.status IN ('prepared', 'in_flight')
                          )
                        FOR UPDATE OF call SKIP LOCKED
                    ), totals AS (
                        SELECT orphaned.call_id,
                               COALESCE(sum(attempt.input_tokens), 0)::INTEGER
                                   AS input_tokens,
                               COALESCE(sum(attempt.output_tokens), 0)::INTEGER
                                   AS output_tokens,
                               COALESCE(
                                   sum(attempt.charged_cost_usd_micros), 0
                               )::BIGINT AS cost_usd_micros
                        FROM orphaned_calls AS orphaned
                        LEFT JOIN llm_provider_attempts AS attempt
                          ON attempt.call_id = orphaned.call_id
                        GROUP BY orphaned.call_id
                    ), terminalized AS (
                        UPDATE llm_calls AS call
                        SET status = 'cancelled',
                            input_tokens = totals.input_tokens,
                            output_tokens = totals.output_tokens,
                            cost_usd_micros = totals.cost_usd_micros,
                            error_classification = 'cancelled',
                            error_message =
                                'Owning execution ended before LLM completion',
                            completed_at = now()
                        FROM totals
                        WHERE call.call_id = totals.call_id
                        RETURNING call.call_id
                    )
                    SELECT count(*) FROM terminalized
                    """
                )
                or 0
            )

    return LlmReconciliationResult(
        prepared_blocked=sum(
            str(row["previous_status"]) == "prepared" for row in reconciled
        ),
        in_flight_cancelled=sum(
            str(row["previous_status"]) == "in_flight" for row in reconciled
        ),
        calls_cancelled=calls_cancelled,
    )


async def reconcile_orphaned_llm_attempts_forever(
    pool: Any,
    stop: asyncio.Event,
    *,
    interval_seconds: float = LLM_RECONCILIATION_INTERVAL_SECONDS,
) -> None:
    """Run replica-safe orphan reconciliation until application shutdown."""
    while not stop.is_set():
        try:
            result = await reconcile_orphaned_llm_attempts(pool)
            if result.prepared_blocked or result.in_flight_cancelled:
                logger.warning(
                    "Reconciled %d pre-egress and %d unknown-outcome LLM attempt(s)",
                    result.prepared_blocked,
                    result.in_flight_cancelled,
                )
        except Exception:
            logger.exception("LLM attempt reconciliation failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass


def _provider_policy(row: Any) -> ProviderPolicy:
    return ProviderPolicy(
        provider=cast(ProviderName, str(row["provider"])),
        model=str(row["model"]),
        endpoint_url=str(row["endpoint_url"]),
        data_residency=str(row["data_residency"]),
        allowed_data_classifications=frozenset(
            str(value) for value in row["allowed_data_classifications"]
        ),
        input_cost_per_million_tokens_usd_micros=int(
            row["input_cost_per_million_tokens_usd_micros"]
        ),
        output_cost_per_million_tokens_usd_micros=int(
            row["output_cost_per_million_tokens_usd_micros"]
        ),
    )


async def _assert_execution_active(conn: Any, context: LlmRequestContext) -> None:
    owner_id = context.execution_owner_id
    if owner_id is None or (
        context.execution_kind == "custom_agent_test" and owner_id != context.run_id
    ):
        raise LlmRunInactiveError(
            f"LLM execution {context.execution_kind}/{context.run_id} has no valid owner"
        )
    if context.execution_kind == "agent_run":
        row = await conn.fetchrow(
            """
            SELECT 1
            FROM agent_runs
            WHERE run_id = $1 AND project_id = $2
              AND status IN ('started', 'running')
              AND lease_owner_id = $3
              AND lease_expires_at > now()
            FOR SHARE
            """,
            context.run_id,
            context.project_id,
            owner_id,
        )
    else:
        row = await conn.fetchrow(
            """
            SELECT 1
            FROM custom_agent_test_runs
            WHERE test_run_id = $1 AND project_id = $2
              AND status = 'running'
              AND lease_expires_at > now()
            FOR SHARE
            """,
            context.run_id,
            context.project_id,
        )
    if row is None:
        raise LlmRunInactiveError(
            f"LLM execution {context.execution_kind}/{context.run_id} is not active "
            f"for project {context.project_id}"
        )


def _unavailable(
    context: LlmRequestContext, operation: str, exc: Exception
) -> NoReturn:
    raise LlmGovernanceUnavailableError(
        f"LLM governance store unavailable while {operation} for "
        f"{context.project_id}/{context.run_id}"
    ) from exc


async def load_project_llm_policy(context: LlmRequestContext) -> ProjectLlmPolicy:
    """Load the explicit project/provider policy; a missing row fails closed."""
    try:
        async with context.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT project_id, required_data_residency,
                       allow_cross_vendor_retry,
                       project_daily_cost_limit_usd_micros,
                       run_cost_limit_usd_micros
                FROM llm_project_policies
                WHERE project_id = $1
                """,
                context.project_id,
            )
            if row is None:
                raise LlmPolicyDeniedError(
                    f"No LLM policy exists for project {context.project_id}"
                )
            provider_rows = await conn.fetch(
                """
                SELECT provider, model, endpoint_url, data_residency,
                       allowed_data_classifications,
                       input_cost_per_million_tokens_usd_micros,
                       output_cost_per_million_tokens_usd_micros
                FROM llm_project_provider_policies
                WHERE project_id = $1 AND enabled = TRUE
                ORDER BY provider, model
                """,
                context.project_id,
            )
        return ProjectLlmPolicy(
            project_id=str(row["project_id"]),
            required_data_residency=str(row["required_data_residency"]),
            allow_cross_vendor_retry=bool(row["allow_cross_vendor_retry"]),
            project_daily_cost_limit_usd_micros=int(
                row["project_daily_cost_limit_usd_micros"]
            ),
            run_cost_limit_usd_micros=int(row["run_cost_limit_usd_micros"]),
            providers=tuple(_provider_policy(item) for item in provider_rows),
        )
    except LlmGovernanceError:
        raise
    except Exception as exc:
        _unavailable(context, "loading policy", exc)


async def begin_llm_call(
    context: LlmRequestContext,
    *,
    prompt_sha256: str,
) -> UUID:
    """Persist one logical call only while its owning execution is active."""
    call_id = uuid4()
    try:
        async with context.pool.acquire() as conn:
            async with conn.transaction():
                await _assert_execution_active(conn, context)
                await conn.execute(
                    """
                    INSERT INTO llm_calls (
                        call_id, project_id, run_id, execution_kind,
                        execution_owner_id, purpose, data_classification,
                        prompt_sha256
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    """,
                    call_id,
                    context.project_id,
                    context.run_id,
                    context.execution_kind,
                    context.execution_owner_id,
                    context.purpose,
                    context.data_classification,
                    prompt_sha256,
                )
        return call_id
    except LlmGovernanceError:
        raise
    except Exception as exc:
        _unavailable(context, "creating a logical call", exc)


async def prepare_provider_attempt(
    context: LlmRequestContext,
    *,
    call_id: UUID,
    attempt_number: int,
    provider: ProviderName,
    model: str,
    endpoint_url: str,
    prompt_sha256: str,
    estimated_input_tokens: int,
    max_output_tokens: int,
) -> PreparedLlmAttempt:
    """Atomically authorize policy, reserve both budgets, and log pre-egress."""
    attempt_id = uuid4()
    try:
        async with context.pool.acquire() as conn:
            async with conn.transaction():
                # Every replica takes the same project-then-run lock order.
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    f"apdl:llm-budget:project:{context.project_id}",
                )
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    f"apdl:llm-budget:run:{context.project_id}:{context.run_id}",
                )
                await _assert_execution_active(conn, context)

                call_status = await conn.fetchval(
                    """
                    SELECT status
                    FROM llm_calls
                    WHERE call_id = $1 AND project_id = $2 AND run_id = $3
                      AND execution_owner_id = $4
                    FOR UPDATE
                    """,
                    call_id,
                    context.project_id,
                    context.run_id,
                    context.execution_owner_id,
                )
                if call_status not in {"prepared", "in_flight"}:
                    raise LlmPolicyDeniedError(
                        f"Logical LLM call {call_id} is not active"
                    )

                policy_row = await conn.fetchrow(
                    """
                    SELECT policy.required_data_residency,
                           policy.allow_cross_vendor_retry,
                           policy.project_daily_cost_limit_usd_micros,
                           policy.run_cost_limit_usd_micros,
                           provider.provider,
                           provider.model,
                           provider.endpoint_url,
                           provider.data_residency,
                           provider.allowed_data_classifications,
                           provider.input_cost_per_million_tokens_usd_micros,
                           provider.output_cost_per_million_tokens_usd_micros
                    FROM llm_project_policies AS policy
                    JOIN llm_project_provider_policies AS provider
                      ON provider.project_id = policy.project_id
                    WHERE policy.project_id = $1
                      AND provider.provider = $2
                      AND provider.model = $3
                      AND provider.endpoint_url = $4
                      AND provider.enabled = TRUE
                    FOR SHARE OF policy, provider
                    """,
                    context.project_id,
                    provider,
                    model,
                    endpoint_url,
                )
                if policy_row is None:
                    raise LlmPolicyDeniedError(
                        f"Provider/model {provider}/{model} is not enabled for "
                        f"project {context.project_id}"
                    )
                provider_policy = _provider_policy(policy_row)
                if not provider_policy.permits(
                    context, str(policy_row["required_data_residency"])
                ):
                    raise LlmPolicyDeniedError(
                        f"Provider/model {provider}/{model} does not permit "
                        f"{context.data_classification} data in required residency "
                        f"{policy_row['required_data_residency']}"
                    )

                previous_attempt = await conn.fetchrow(
                    """
                    SELECT attempt_number, provider, status, retryable
                    FROM llm_provider_attempts
                    WHERE call_id = $1
                    ORDER BY attempt_number DESC
                    LIMIT 1
                    """,
                    call_id,
                )
                if previous_attempt is None:
                    if attempt_number != 1:
                        raise LlmPolicyDeniedError(
                            "The first provider attempt must use attempt_number 1"
                        )
                else:
                    expected_number = int(previous_attempt["attempt_number"]) + 1
                    if attempt_number != expected_number:
                        raise LlmPolicyDeniedError(
                            f"Provider attempt_number must be {expected_number}"
                        )
                    if str(previous_attempt["status"]) != "failed" or not bool(
                        previous_attempt["retryable"]
                    ):
                        raise LlmPolicyDeniedError(
                            "A provider retry requires a classified retryable failure"
                        )
                    if provider != str(previous_attempt["provider"]) and not bool(
                        policy_row["allow_cross_vendor_retry"]
                    ):
                        raise LlmPolicyDeniedError(
                            "Cross-vendor retry is disabled by project policy"
                        )

                reserved_cost = cost_usd_micros(
                    input_tokens=estimated_input_tokens,
                    output_tokens=max_output_tokens,
                    policy=provider_policy,
                )
                project_used = int(
                    await conn.fetchval(
                        """
                        SELECT COALESCE(sum(COALESCE(
                            charged_cost_usd_micros,
                            reserved_cost_usd_micros
                        )), 0)
                        FROM llm_provider_attempts
                        WHERE project_id = $1
                          AND prepared_at >= date_trunc(
                              'day', now() AT TIME ZONE 'UTC'
                          ) AT TIME ZONE 'UTC'
                        """,
                        context.project_id,
                    )
                    or 0
                )
                run_used = int(
                    await conn.fetchval(
                        """
                        SELECT COALESCE(sum(COALESCE(
                            charged_cost_usd_micros,
                            reserved_cost_usd_micros
                        )), 0)
                        FROM llm_provider_attempts
                        WHERE project_id = $1 AND run_id = $2
                        """,
                        context.project_id,
                        context.run_id,
                    )
                    or 0
                )
                project_limit = int(policy_row["project_daily_cost_limit_usd_micros"])
                run_limit = int(policy_row["run_cost_limit_usd_micros"])
                if project_used + reserved_cost > project_limit:
                    raise LlmBudgetExceededError(
                        f"Project daily LLM cost ceiling exceeded: "
                        f"{project_used}+{reserved_cost}>{project_limit}"
                    )
                if run_used + reserved_cost > run_limit:
                    raise LlmBudgetExceededError(
                        f"Run LLM cost ceiling exceeded: "
                        f"{run_used}+{reserved_cost}>{run_limit}"
                    )

                await conn.execute(
                    """
                    INSERT INTO llm_provider_attempts (
                        attempt_id, call_id, project_id, run_id, attempt_number,
                        execution_owner_id, provider, model, endpoint_url, prompt_sha256,
                        estimated_input_tokens, max_output_tokens,
                        reserved_cost_usd_micros
                    )
                    VALUES (
                        $1, $2, $3, $4, $5, $6, $7,
                        $8, $9, $10, $11, $12, $13
                    )
                    """,
                    attempt_id,
                    call_id,
                    context.project_id,
                    context.run_id,
                    attempt_number,
                    context.execution_owner_id,
                    provider,
                    model,
                    endpoint_url,
                    prompt_sha256,
                    estimated_input_tokens,
                    max_output_tokens,
                    reserved_cost,
                )
                await conn.execute(
                    """
                    UPDATE llm_calls
                    SET status = 'in_flight', attempt_count = attempt_count + 1
                    WHERE call_id = $1
                    """,
                    call_id,
                )
        return PreparedLlmAttempt(
            attempt_id=attempt_id,
            reserved_cost_usd_micros=reserved_cost,
            provider_policy=provider_policy,
        )
    except LlmGovernanceError:
        raise
    except Exception as exc:
        _unavailable(context, "reserving provider cost", exc)


async def mark_provider_egress(
    context: LlmRequestContext,
    *,
    attempt_id: UUID,
) -> None:
    """Durably mark the exact point immediately before provider egress."""
    try:
        async with context.pool.acquire() as conn:
            async with conn.transaction():
                await _assert_execution_active(conn, context)
                updated = await conn.fetchval(
                    """
                    UPDATE llm_provider_attempts
                    SET status = 'in_flight', egress_started_at = now()
                    WHERE attempt_id = $1
                      AND project_id = $2
                      AND run_id = $3
                      AND execution_owner_id = $4
                      AND status = 'prepared'
                    RETURNING attempt_id
                    """,
                    attempt_id,
                    context.project_id,
                    context.run_id,
                    context.execution_owner_id,
                )
                if updated is None:
                    raise LlmPolicyDeniedError(
                        f"Provider attempt {attempt_id} is not prepared"
                    )
    except LlmGovernanceError:
        raise
    except Exception as exc:
        _unavailable(context, "marking provider egress", exc)


async def block_provider_attempt_before_egress(
    context: LlmRequestContext,
    *,
    attempt_id: UUID,
    error_classification: ErrorClassification,
    error_message: str,
) -> None:
    """Terminalize and release a prepared reservation that never crossed egress."""
    try:
        async with context.pool.acquire() as conn:
            updated = await conn.fetchval(
                """
                UPDATE llm_provider_attempts
                SET status = 'blocked',
                    charged_cost_usd_micros = 0,
                    retryable = FALSE,
                    error_classification = $2,
                    error_message = $3,
                    completed_at = now()
                WHERE attempt_id = $1
                  AND project_id = $4
                  AND run_id = $5
                  AND execution_owner_id = $6
                  AND status = 'prepared'
                RETURNING attempt_id
                """,
                attempt_id,
                error_classification,
                error_message[:4_000],
                context.project_id,
                context.run_id,
                context.execution_owner_id,
            )
            if updated is None:
                raise LlmPolicyDeniedError(
                    f"Provider attempt {attempt_id} is not prepared"
                )
    except LlmGovernanceError:
        raise
    except Exception as exc:
        _unavailable(context, "blocking pre-egress provider attempt", exc)


async def finish_provider_attempt(
    context: LlmRequestContext,
    *,
    attempt: PreparedLlmAttempt,
    status: Literal["succeeded", "failed", "cancelled"],
    latency_ms: int,
    input_tokens: int | None,
    output_tokens: int | None,
    error_classification: ErrorClassification | None = None,
    error_message: str | None = None,
    retryable: bool = False,
) -> int:
    """Persist provider outcome/usage/cost and retain reserved cost if unknown."""
    if status not in _TERMINAL_ATTEMPT_STATUSES:
        raise ValueError(f"Unknown terminal provider status {status!r}")
    if status == "succeeded" and error_classification is not None:
        raise ValueError("A succeeded provider attempt cannot have an error")
    if status != "succeeded" and error_classification is None:
        raise ValueError(
            "A failed or cancelled provider attempt requires classification"
        )

    actual_cost = (
        cost_usd_micros(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            policy=attempt.provider_policy,
        )
        if input_tokens is not None and output_tokens is not None
        else attempt.reserved_cost_usd_micros
    )
    cost_overrun = actual_cost > attempt.reserved_cost_usd_micros
    if cost_overrun:
        status = "failed"
        error_classification = "cost_overrun"
        error_message = (
            f"Provider usage cost {actual_cost} exceeded reservation "
            f"{attempt.reserved_cost_usd_micros}"
        )
        retryable = False
    charged_cost = (
        actual_cost if status == "succeeded" else attempt.reserved_cost_usd_micros
    )
    if cost_overrun:
        charged_cost = actual_cost

    try:
        async with context.pool.acquire() as conn:
            updated = await conn.fetchval(
                """
                UPDATE llm_provider_attempts
                SET status = $2,
                    input_tokens = $3,
                    output_tokens = $4,
                    charged_cost_usd_micros = $5,
                    latency_ms = $6,
                    retryable = $7,
                    error_classification = $8,
                    error_message = $9,
                    completed_at = now()
                WHERE attempt_id = $1
                  AND project_id = $10
                  AND run_id = $11
                  AND execution_owner_id = $12
                  AND status = 'in_flight'
                RETURNING attempt_id
                """,
                attempt.attempt_id,
                status,
                input_tokens,
                output_tokens,
                charged_cost,
                max(0, latency_ms),
                retryable,
                error_classification,
                error_message[:4_000] if error_message is not None else None,
                context.project_id,
                context.run_id,
                context.execution_owner_id,
            )
            if updated is None:
                raise LlmPolicyDeniedError(
                    f"Provider attempt {attempt.attempt_id} is not in flight"
                )
    except LlmGovernanceError:
        raise
    except Exception as exc:
        _unavailable(context, "finalizing provider audit", exc)

    if cost_overrun:
        raise LlmCostOverrunError(error_message or "LLM cost reservation overrun")
    return charged_cost


async def finish_llm_call(
    context: LlmRequestContext,
    *,
    call_id: UUID,
    status: Literal["succeeded", "failed", "cancelled", "blocked"],
    error_classification: ErrorClassification | None = None,
    error_message: str | None = None,
) -> None:
    """Terminalize one logical call from its immutable provider-attempt ledger."""
    if status == "succeeded" and error_classification is not None:
        raise ValueError("A succeeded logical call cannot have an error")
    if status != "succeeded" and error_classification is None:
        raise ValueError("A non-success logical call requires classification")
    try:
        async with context.pool.acquire() as conn:
            updated = await conn.fetchval(
                """
                UPDATE llm_calls AS call
                SET status = $2,
                    input_tokens = totals.input_tokens,
                    output_tokens = totals.output_tokens,
                    cost_usd_micros = totals.cost_usd_micros,
                    error_classification = $3,
                    error_message = $4,
                    completed_at = now()
                FROM (
                    SELECT COALESCE(sum(input_tokens), 0)::INTEGER AS input_tokens,
                           COALESCE(sum(output_tokens), 0)::INTEGER AS output_tokens,
                           COALESCE(sum(charged_cost_usd_micros), 0)::BIGINT
                               AS cost_usd_micros
                    FROM llm_provider_attempts
                    WHERE call_id = $1
                ) AS totals
                WHERE call.call_id = $1
                  AND call.project_id = $5
                  AND call.run_id = $6
                  AND call.execution_owner_id = $7
                  AND call.status IN ('prepared', 'in_flight')
                RETURNING call.call_id
                """,
                call_id,
                status,
                error_classification,
                error_message[:4_000] if error_message is not None else None,
                context.project_id,
                context.run_id,
                context.execution_owner_id,
            )
            if updated is None:
                raise LlmPolicyDeniedError(f"Logical LLM call {call_id} is not active")
    except LlmGovernanceError:
        raise
    except Exception as exc:
        _unavailable(context, "finalizing logical call", exc)
