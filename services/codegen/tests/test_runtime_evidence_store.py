from datetime import UTC, datetime, timedelta

import pytest

from app.models.observations import CIVerificationObservation, ExternalCIStatus
from app.runtime.collector import RuntimeEvidenceCollection
from app.runtime.evidence import build_runtime_evidence_observation
from app.runtime.models import RuntimeAcceptancePlan
from app.store import changesets as changeset_store
from app.store.runtime_evidence import (
    apply_runtime_evidence_observation,
    list_runtime_evidence_observations,
)
from tests.fakes import FakePool

NOW = datetime(2026, 7, 11, 12, 0, tzinfo=UTC)


def _plan() -> RuntimeAcceptancePlan:
    return RuntimeAcceptancePlan(
        source_ledger_sha256="a" * 64,
        repo_profile_sha256="b" * 64,
        verification_plan_sha256="c" * 64,
        repo="acme/widgets",
        branch="apdl/change",
    )


def _ci(head_sha: str) -> CIVerificationObservation:
    return CIVerificationObservation(
        observation_id="ciobs_" + "d" * 32,
        changeset_id="cs-1",
        repository="acme/widgets",
        pr_number=17,
        head_sha=head_sha,
        status=ExternalCIStatus.unverified_external_ci,
        observed_at=NOW,
    )


def _observation(*, head_sha: str = "head-a", observed_at: datetime = NOW):
    return build_runtime_evidence_observation(
        changeset_id="cs-1",
        repository="acme/widgets",
        pr_number=17,
        head_sha=head_sha,
        ci_observation=_ci(head_sha),
        plan=_plan(),
        collection=RuntimeEvidenceCollection(head_sha=head_sha),
        observed_at=observed_at,
    )


def _pool() -> FakePool:
    pool = FakePool()
    pool.add_connection("demo", "acme/widgets")
    pool.add_changeset(
        "cs-1",
        status="pr_open",
        branch="apdl/change",
        pr_number=17,
        head_sha="head-a",
        github_pr_status="open",
        external_ci_status="unverified_external_ci",
    )
    pool.store["changesets"]["cs-1"]["runtime_acceptance_plan"] = _plan().model_dump(
        mode="json"
    )
    return pool


@pytest.mark.asyncio
async def test_runtime_evidence_is_append_only_and_projects_without_changing_ci():
    pool = _pool()
    observation = _observation()

    first = await apply_runtime_evidence_observation(pool, observation)
    duplicate = await apply_runtime_evidence_observation(
        pool, _observation(observed_at=NOW + timedelta(minutes=1))
    )

    assert first.projected is True
    assert duplicate.reason == "duplicate"
    changeset = await changeset_store.get_changeset(pool, "cs-1")
    assert changeset is not None
    assert changeset.status.value == "pr_open"
    assert changeset.external_ci_status is ExternalCIStatus.unverified_external_ci
    assert changeset.runtime_evidence_assessment is not None
    assert len(await list_runtime_evidence_observations(pool, "cs-1")) == 1


@pytest.mark.asyncio
async def test_stale_runtime_head_is_journaled_but_never_projected():
    pool = _pool()
    stale = _observation(head_sha="head-old")

    result = await apply_runtime_evidence_observation(pool, stale)

    assert result.inserted is True
    assert result.projected is False
    assert result.reason == "stale_or_ineligible_head"
    changeset = await changeset_store.get_changeset(pool, "cs-1")
    assert changeset is not None
    assert changeset.runtime_evidence_assessment is None


@pytest.mark.asyncio
async def test_runtime_evidence_for_a_different_plan_is_never_projected():
    pool = _pool()
    pool.store["changesets"]["cs-1"]["runtime_acceptance_plan"] = (
        RuntimeAcceptancePlan(
            source_ledger_sha256="d" * 64,
            repo_profile_sha256="e" * 64,
            verification_plan_sha256="f" * 64,
        ).model_dump(mode="json")
    )

    result = await apply_runtime_evidence_observation(pool, _observation())

    assert result.inserted is True
    assert result.projected is False
    assert result.reason == "stale_or_ineligible_head"
