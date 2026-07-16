"""Strict contracts for governed LLM requests and provider outcomes."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

import asyncpg


ProviderName = Literal["openai", "anthropic", "google", "local"]
DataClassification = Literal["public", "internal", "confidential", "restricted"]
ExecutionKind = Literal["agent_run", "custom_agent_test"]
ErrorClassification = Literal[
    "timeout",
    "network",
    "rate_limited",
    "provider_unavailable",
    "authentication",
    "permission",
    "invalid_request",
    "model_not_found",
    "safety_block",
    "policy_denied",
    "budget_exceeded",
    "run_inactive",
    "cost_overrun",
    "no_provider",
    "cancelled",
    "governance_unavailable",
    "unknown",
]

_PROJECT_ID = re.compile(r"^[A-Za-z0-9]{1,64}$")
_PURPOSE = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_RUN_ID = re.compile(r"^\S{1,128}$")
_EXECUTION_OWNER_ID = re.compile(r"^\S{1,512}$")
_DATA_CLASSIFICATIONS = frozenset({"public", "internal", "confidential", "restricted"})


class LlmGovernanceError(RuntimeError):
    """Base class for fail-closed LLM governance decisions."""


class LlmGovernanceUnavailableError(LlmGovernanceError):
    """The authoritative policy/audit store could not make a decision."""


class LlmPolicyDeniedError(LlmGovernanceError):
    """The project policy forbids this request or candidate provider."""


class LlmBudgetExceededError(LlmGovernanceError):
    """The atomic project or run cost reservation exceeded its ceiling."""


class LlmRunInactiveError(LlmGovernanceError):
    """The owning run is missing, terminal, or belongs to another project."""


class LlmCostOverrunError(LlmGovernanceError):
    """Provider-reported usage exceeded the conservative cost reservation."""


@dataclass(frozen=True)
class LlmRequestContext:
    """The required tenant, execution, purpose, and privacy scope for one call."""

    pool: asyncpg.Pool
    project_id: str
    run_id: str
    execution_kind: ExecutionKind
    purpose: str
    data_classification: DataClassification
    execution_owner_id: str | None = None

    def __post_init__(self) -> None:
        if not _PROJECT_ID.fullmatch(self.project_id):
            raise ValueError("project_id must match ^[A-Za-z0-9]{1,64}$")
        if not _RUN_ID.fullmatch(self.run_id):
            raise ValueError("run_id must be 1 to 128 non-whitespace characters")
        if self.execution_kind not in {"agent_run", "custom_agent_test"}:
            raise ValueError("execution_kind must be agent_run or custom_agent_test")
        if self.execution_owner_id is not None and not _EXECUTION_OWNER_ID.fullmatch(
            self.execution_owner_id
        ):
            raise ValueError(
                "execution_owner_id must be 1 to 512 non-whitespace characters"
            )
        if not _PURPOSE.fullmatch(self.purpose):
            raise ValueError("purpose must match ^[a-z][a-z0-9_.:-]{0,127}$")
        if self.data_classification not in _DATA_CLASSIFICATIONS:
            raise ValueError(
                f"Unknown data classification {self.data_classification!r}"
            )


@dataclass(frozen=True)
class ProviderPolicy:
    """One explicitly permitted provider/model/privacy/pricing combination."""

    provider: ProviderName
    model: str
    endpoint_url: str
    data_residency: str
    allowed_data_classifications: frozenset[str]
    input_cost_per_million_tokens_usd_micros: int
    output_cost_per_million_tokens_usd_micros: int

    def permits(self, context: LlmRequestContext, required_residency: str) -> bool:
        return (
            self.data_residency == required_residency
            and context.data_classification in self.allowed_data_classifications
        )


@dataclass(frozen=True)
class ProjectLlmPolicy:
    """Authoritative project budget and provider egress policy."""

    project_id: str
    required_data_residency: str
    allow_cross_vendor_retry: bool
    project_daily_cost_limit_usd_micros: int
    run_cost_limit_usd_micros: int
    providers: tuple[ProviderPolicy, ...]

    def provider_policy(
        self,
        context: LlmRequestContext,
        provider: str,
        model: str,
        endpoint_url: str,
    ) -> ProviderPolicy | None:
        return next(
            (
                item
                for item in self.providers
                if item.provider == provider
                and item.model == model
                and item.endpoint_url == endpoint_url
                and item.permits(context, self.required_data_residency)
            ),
            None,
        )


@dataclass(frozen=True)
class PreparedLlmAttempt:
    """A durable, budgeted provider attempt that has not crossed egress yet."""

    attempt_id: UUID
    reserved_cost_usd_micros: int
    provider_policy: ProviderPolicy


@dataclass(frozen=True)
class ProviderErrorDisposition:
    classification: ErrorClassification
    retryable: bool


def _status_code(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        status = getattr(exc, "status", None)
    if not isinstance(status, int):
        status = getattr(exc, "code", None)
    if not isinstance(status, int):
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def classify_provider_error(exc: Exception) -> ProviderErrorDisposition:
    """Classify provider failures; unknown failures are never retryable."""
    name = type(exc).__name__.lower()
    status = _status_code(exc)

    if isinstance(exc, (TimeoutError,)) or "timeout" in name:
        return ProviderErrorDisposition("timeout", True)
    if isinstance(exc, ConnectionError) or "connection" in name:
        return ProviderErrorDisposition("network", True)
    if status == 429:
        return ProviderErrorDisposition("rate_limited", True)
    if status in {408, 500, 502, 503, 504}:
        return ProviderErrorDisposition("provider_unavailable", True)
    if status == 401:
        return ProviderErrorDisposition("authentication", False)
    if status == 403:
        return ProviderErrorDisposition("permission", False)
    if status == 404:
        return ProviderErrorDisposition("model_not_found", False)
    if status in {400, 409, 422}:
        return ProviderErrorDisposition("invalid_request", False)
    if "contentfilter" in name or "safety" in name:
        return ProviderErrorDisposition("safety_block", False)
    return ProviderErrorDisposition("unknown", False)


def canonical_prompt_bytes(
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
) -> bytes:
    """Serialize the provider-neutral prompt deterministically for audit hashing."""

    def encode(value: Any) -> Any:
        if isinstance(value, bytes):
            return {"bytes_hex": value.hex()}
        raise TypeError(f"Unsupported prompt value {type(value).__name__}")

    return json.dumps(
        {"messages": messages, "tools": tools or []},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=encode,
    ).encode("utf-8")


def prompt_sha256(prompt: bytes) -> str:
    return hashlib.sha256(prompt).hexdigest()


def conservative_input_token_bound(prompt: bytes) -> int:
    """Use UTF-8 bytes as a tokenizer-independent upper bound for input tokens."""
    return max(1, len(prompt))


def cost_usd_micros(
    *,
    input_tokens: int,
    output_tokens: int,
    policy: ProviderPolicy,
) -> int:
    """Calculate cost with integer ceiling arithmetic at configured model rates."""
    numerator = (
        input_tokens * policy.input_cost_per_million_tokens_usd_micros
        + output_tokens * policy.output_cost_per_million_tokens_usd_micros
    )
    return (numerator + 999_999) // 1_000_000
