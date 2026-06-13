"""Canonical envelope for decisions emitted by the Config service.

Every flag evaluation and experiment exposure produced by Config is wrapped
in this envelope and published to the `decisions:raw:{project_id}` Redis
Stream. The ClickHouse writer drains the stream into `decisions_v2`.

This is *additive* to the existing EvalResult model — the SSE/HTTP API
contract returned to the SDK stays as it is. The envelope is what gets
persisted to the analytical spine, not what the SDK consumes.
"""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


# ---------- payloads ----------

class FlagEvalPayload(BaseModel):
    """A single flag evaluation. Emitted every time Config evaluates a flag."""

    model_config = ConfigDict(extra="forbid")

    flag_key: str = Field(min_length=1, max_length=256)
    user_id: str = ""
    anonymous_id: str = ""
    session_id: str = ""

    enabled: bool
    variant: str = ""
    value: str = ""
    reason: str = ""                               # 'rule_match' | 'default' | 'rollout' | 'disabled' | ...
    rule_id: str = ""
    rollout_bucket: int = Field(default=0, ge=0, le=9999)


class ExposurePayload(BaseModel):
    """An experiment exposure — the user was assigned a variant in a running
    experiment. Emitted at most once per (user, experiment) per window;
    deduped downstream by _idempotency_key."""

    model_config = ConfigDict(extra="forbid")

    experiment_key: str = Field(min_length=1, max_length=256)
    flag_key: str = ""                             # linked feature flag (if any)
    user_id: str = ""
    anonymous_id: str = ""
    session_id: str = ""

    variant: str = Field(min_length=1, max_length=128)
    rollout_bucket: int = Field(default=0, ge=0, le=9999)
    targeting: dict[str, Any] = Field(default_factory=dict)   # rule attributes that matched


class PersonalizationPayload(BaseModel):
    """A runtime UI/content variant selection (ui_configs slot)."""

    model_config = ConfigDict(extra="forbid")

    slot_id: str = Field(min_length=1, max_length=256)
    component_name: str = Field(min_length=1, max_length=256)
    user_id: str = ""
    anonymous_id: str = ""
    session_id: str = ""

    variant: str = ""
    experiment_id: str | None = None
    props: dict[str, Any] = Field(default_factory=dict)


# ---------- envelope ----------

_DecisionSchema = Literal[
    "flag_eval@1",
    "exposure@1",
    "personalization@1",
]


class DecisionEnvelope(BaseModel):
    """Canonical envelope for any decision emitted by Config."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: UUID = Field(alias="_id")
    schema_: _DecisionSchema = Field(alias="_schema")
    project_id: int = Field(alias="_project_id", ge=1)
    idempotency_key: str = Field(alias="_idempotency_key", min_length=1, max_length=128)
    correlation_id: UUID | None = Field(default=None, alias="_correlation_id")
    source: str = Field(alias="_source", min_length=1, max_length=64)
    occurred_at: datetime = Field(alias="_occurred_at")

    # Undiscriminated union: the discriminator lives on the envelope's `_schema`.
    payload: FlagEvalPayload | ExposurePayload | PersonalizationPayload


# ---------- idempotency helpers ----------

def flag_eval_idempotency_key(project_id: int, flag_key: str, user_or_anon: str, bucket: int) -> str:
    """Stable key so re-evaluating the same flag for the same user in the
    same bucket window collapses to one row in decisions_v2."""
    return f"feval:{project_id}:{flag_key}:{user_or_anon}:{bucket}"


def exposure_idempotency_key(project_id: int, experiment_key: str, user_or_anon: str) -> str:
    """One exposure per (experiment, user) — ever. Re-runs are no-ops in CH."""
    return f"expo:{project_id}:{experiment_key}:{user_or_anon}"


def personalization_idempotency_key(project_id: int, slot_id: str, user_or_anon: str, variant: str) -> str:
    return f"pers:{project_id}:{slot_id}:{user_or_anon}:{variant}"
