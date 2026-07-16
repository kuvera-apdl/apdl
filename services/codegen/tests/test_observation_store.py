"""Focused append-only observation-store tests with a dedicated minimal fake."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.models.observations import (
    CIRemediationAttempt,
    CISignal,
    CISignalConclusion,
    CISignalKind,
    CIVerificationObservation,
    ExternalCIStatus,
    FailureClassification,
    GitHubPRStatus,
    PullRequestObservation,
    RemediationDisposition,
)
from app.store.observations import (
    ObservationDecodeError,
    insert_ci_remediation_attempt,
    insert_ci_verification_observation,
    insert_pull_request_observation,
    latest_ci_remediation_attempt,
    latest_ci_verification_observation,
    latest_pull_request_observation,
    list_ci_remediation_attempts,
    list_ci_verification_observations,
    list_pull_request_observations,
)

_T0 = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)


class _Acquire:
    def __init__(self, connection: "_ObservationConn") -> None:
        self.connection = connection

    async def __aenter__(self) -> "_ObservationConn":
        return self.connection

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _ObservationConn:
    """Implements only the exact immutable SQL contract exercised here."""

    def __init__(self) -> None:
        self.pr: list[dict[str, Any]] = []
        self.ci: list[dict[str, Any]] = []
        self.remediation: list[dict[str, Any]] = []
        self.claims: set[tuple[str, str, str]] = set()
        self.queries: list[str] = []

    async def fetchval(self, query: str, *args: Any):
        self.queries.append(query)
        if "INSERT INTO codegen_pull_request_observations" in query:
            row = {
                "observation_id": args[0],
                "delivery_id": args[1],
                "changeset_id": args[2],
                "repository": args[3],
                "pr_number": args[4],
                "head_sha": args[5],
                "status": args[6],
                "github_updated_at": args[7],
                "observed_at": args[8],
                "payload": args[9],
            }
            if any(
                item["observation_id"] == row["observation_id"]
                or (
                    row["delivery_id"] is not None
                    and item["delivery_id"] == row["delivery_id"]
                )
                for item in self.pr
            ):
                return None
            self.pr.append(row)
            return row["observation_id"]
        if "INSERT INTO codegen_ci_verification_observations" in query:
            row = {
                "observation_id": args[0],
                "changeset_id": args[1],
                "repository": args[2],
                "pr_number": args[3],
                "head_sha": args[4],
                "status": args[5],
                "evidence_hash": args[6],
                "observed_at": args[7],
                "payload": args[8],
            }
            if any(
                item["observation_id"] == row["observation_id"]
                or (
                    item["changeset_id"] == row["changeset_id"]
                    and item["head_sha"] == row["head_sha"]
                    and item["evidence_hash"] == row["evidence_hash"]
                )
                for item in self.ci
            ):
                return None
            self.ci.append(row)
            return row["observation_id"]
        if "INSERT INTO codegen_ci_remediation_attempts" in query:
            row = {
                "event_id": args[0],
                "attempt_id": args[1],
                "event_sequence": args[2],
                "changeset_id": args[3],
                "repository": args[4],
                "pr_number": args[5],
                "failed_head_sha": args[6],
                "failure_observation_id": args[7],
                "attempt_number": args[8],
                "started_at": args[9],
                "recorded_at": args[10],
                "payload": args[11],
            }
            if any(
                item["event_id"] == row["event_id"]
                or (
                    item["attempt_id"] == row["attempt_id"]
                    and item["event_sequence"] == row["event_sequence"]
                )
                for item in self.remediation
            ):
                return None
            self.remediation.append(row)
            return row["event_id"]
        if "INSERT INTO codegen_ci_remediation_claims" in query:
            (
                changeset_id,
                failed_head,
                failure_id,
                claim_scope,
                current_head,
                current_status,
            ) = args
            key = (changeset_id, failed_head, claim_scope)
            matching_prs = [item for item in self.pr if item["changeset_id"] == changeset_id]
            latest_pr = max(
                matching_prs,
                key=lambda item: (
                    item["github_updated_at"],
                    item["observed_at"],
                    item["observation_id"],
                ),
                default=None,
            )
            failed_ci_exists = False
            for item in self.ci:
                if not (
                    item["observation_id"] == failure_id
                    and item["changeset_id"] == changeset_id
                    and item["head_sha"] == failed_head
                    and item["status"] == "failed"
                ):
                    continue
                signals = json.loads(item["payload"])["signals"]
                failed_ci_exists = any(
                    signal["conclusion"] == "failed"
                    and (
                        (
                            claim_scope.startswith("check_suite:")
                            and str(signal["check_suite_id"])
                            == claim_scope.split(":", 1)[1]
                        )
                        or (
                            not claim_scope.startswith("check_suite:")
                            and signal["signal_id"] == claim_scope
                        )
                    )
                    for signal in signals
                )
            matching_ci = [
                item
                for item in self.ci
                if item["changeset_id"] == changeset_id
                and item["head_sha"] == failed_head
            ]
            latest_ci = max(
                matching_ci,
                key=lambda item: (item["observed_at"], item["observation_id"]),
                default=None,
            )
            if (
                key in self.claims
                or current_head != failed_head
                or current_status not in {"open", "draft"}
                or latest_pr is None
                or latest_pr["head_sha"] != current_head
                or latest_pr["status"] != current_status
                or not failed_ci_exists
                or latest_ci is None
                or latest_ci["observation_id"] != failure_id
            ):
                return None
            self.claims.add(key)
            return changeset_id
        raise AssertionError(f"Unexpected fetchval query: {query}")

    async def fetch(self, query: str, *args: Any):
        self.queries.append(query)
        changeset_id, head_sha, limit = args
        if "FROM codegen_pull_request_observations" in query:
            rows = [
                item
                for item in self.pr
                if item["changeset_id"] == changeset_id
                and (head_sha is None or item["head_sha"] == head_sha)
            ]
            rows.sort(
                key=lambda item: (item["observed_at"], item["observation_id"]),
                reverse=True,
            )
        elif "FROM codegen_ci_verification_observations" in query:
            rows = [
                item
                for item in self.ci
                if item["changeset_id"] == changeset_id
                and (head_sha is None or item["head_sha"] == head_sha)
            ]
            rows.sort(
                key=lambda item: (item["observed_at"], item["observation_id"]),
                reverse=True,
            )
        elif "FROM codegen_ci_remediation_attempts" in query:
            rows = [
                item
                for item in self.remediation
                if item["changeset_id"] == changeset_id
                and (head_sha is None or item["failed_head_sha"] == head_sha)
            ]
            rows.sort(
                key=lambda item: (
                    item["recorded_at"],
                    item["attempt_number"],
                    item["event_sequence"],
                    item["event_id"],
                ),
                reverse=True,
            )
        else:
            raise AssertionError(f"Unexpected fetch query: {query}")
        return [{"payload": item["payload"]} for item in rows[:limit]]


class _ObservationPool:
    def __init__(self) -> None:
        self.conn = _ObservationConn()

    def acquire(self) -> _Acquire:
        return _Acquire(self.conn)


def _pr(
    observation_id: str,
    *,
    delivery_id: str | None,
    head_sha: str = "head-a",
    status: GitHubPRStatus = GitHubPRStatus.open,
    observed_at: datetime = _T0,
) -> PullRequestObservation:
    return PullRequestObservation(
        observation_id=observation_id,
        delivery_id=delivery_id,
        changeset_id="cs-1",
        repository="acme/widgets",
        pr_number=17,
        head_sha=head_sha,
        status=status,
        action="closed" if status is GitHubPRStatus.closed else "opened",
        github_url="https://github.com/acme/widgets/pull/17",
        github_updated_at=observed_at,
        observed_at=observed_at,
    )


def _ci(
    observation_id: str,
    *,
    head_sha: str = "head-a",
    observed_at: datetime = _T0,
    failed: bool = False,
    passed: bool = False,
    check_run_id: int = 7,
) -> CIVerificationObservation:
    if failed:
        signal = CISignal(
            signal_id=f"check_run:{check_run_id}",
            kind=CISignalKind.check_run,
            name="pytest",
            conclusion=CISignalConclusion.failed,
            check_suite_id=2,
            check_run_id=check_run_id,
        )
        return CIVerificationObservation(
            observation_id=observation_id,
            changeset_id="cs-1",
            repository="acme/widgets",
            pr_number=17,
            head_sha=head_sha,
            status=ExternalCIStatus.failed,
            signals=[signal],
            observed_at=observed_at,
            failure_key=f"{head_sha}:check_run:{check_run_id}",
            failure_summary="pytest failed",
        )
    if passed:
        signal = CISignal(
            signal_id=f"check_run:{check_run_id}",
            kind=CISignalKind.check_run,
            name="pytest",
            conclusion=CISignalConclusion.passed,
            check_suite_id=2,
            check_run_id=check_run_id,
        )
        return CIVerificationObservation(
            observation_id=observation_id,
            changeset_id="cs-1",
            repository="acme/widgets",
            pr_number=17,
            head_sha=head_sha,
            status=ExternalCIStatus.passed,
            signals=[signal],
            observed_at=observed_at,
        )
    return CIVerificationObservation(
        observation_id=observation_id,
        changeset_id="cs-1",
        repository="acme/widgets",
        pr_number=17,
        head_sha=head_sha,
        status=ExternalCIStatus.unverified_external_ci,
        observed_at=observed_at,
    )


def _attempt(
    attempt_id: str,
    *,
    head_sha: str = "head-a",
    attempt_number: int = 1,
    started_at: datetime = _T0,
    final: bool = False,
    event_sequence: int = 1,
) -> CIRemediationAttempt:
    finished_at = started_at + timedelta(minutes=2) if final else None
    return CIRemediationAttempt(
        attempt_id=attempt_id,
        event_sequence=event_sequence,
        event_id=f"{attempt_id}:{event_sequence}",
        changeset_id="cs-1",
        repository="acme/widgets",
        pr_number=17,
        failed_head_sha=head_sha,
        failure_observation_id=f"ci-failed-{head_sha}",
        attempt_number=attempt_number,
        classification=FailureClassification.actionable_code,
        confidence=0.95,
        disposition=(
            RemediationDisposition.exhausted
            if final
            else RemediationDisposition.diagnosing
        ),
        started_at=started_at,
        recorded_at=finished_at or started_at,
        finished_at=finished_at,
        error="repair budget exhausted" if final else None,
    )


@pytest.mark.asyncio
async def test_pr_journal_deduplicates_delivery_and_never_overwrites():
    pool = _ObservationPool()
    original = _pr("pr-1", delivery_id="delivery-1")
    duplicate_delivery = _pr(
        "pr-2",
        delivery_id="delivery-1",
        head_sha="head-b",
        observed_at=_T0 + timedelta(minutes=1),
    )

    assert await insert_pull_request_observation(pool, original) is True
    assert await insert_pull_request_observation(pool, original) is False
    assert await insert_pull_request_observation(pool, duplicate_delivery) is False

    values = await list_pull_request_observations(pool, "cs-1")
    assert values == [original]
    assert pool.conn.pr[0]["payload"] == original.model_dump_json()
    assert await latest_pull_request_observation(pool, "cs-1") == original


@pytest.mark.asyncio
async def test_pr_list_and_latest_support_exact_head_and_bounded_limit():
    pool = _ObservationPool()
    first = _pr("pr-1", delivery_id=None, head_sha="head-a")
    second = _pr(
        "pr-2",
        delivery_id=None,
        head_sha="head-b",
        observed_at=_T0 + timedelta(minutes=1),
    )
    await insert_pull_request_observation(pool, first)
    await insert_pull_request_observation(pool, second)

    assert await list_pull_request_observations(pool, "cs-1", limit=1) == [second]
    assert await latest_pull_request_observation(
        pool, "cs-1", head_sha="head-a"
    ) == first
    for bad_limit in (0, 201, True, 1.5):
        with pytest.raises(ValueError, match="limit"):
            await list_pull_request_observations(pool, "cs-1", limit=bad_limit)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_ci_journal_uses_stable_evidence_hash_dedupe_per_exact_head():
    pool = _ObservationPool()
    first = _ci("ci-1")
    repeated_poll = _ci("ci-2", observed_at=_T0 + timedelta(minutes=1))
    other_head = _ci("ci-3", head_sha="head-b")
    assert first.evidence_hash() == repeated_poll.evidence_hash()

    assert await insert_ci_verification_observation(pool, first) is True
    assert await insert_ci_verification_observation(pool, repeated_poll) is False
    assert await insert_ci_verification_observation(pool, other_head) is True

    assert await list_ci_verification_observations(
        pool, "cs-1", head_sha="head-a"
    ) == [first]
    assert await latest_ci_verification_observation(
        pool, "cs-1", head_sha="head-b"
    ) == other_head


@pytest.mark.asyncio
async def test_strict_json_roundtrip_rejects_corrupt_stored_payload():
    pool = _ObservationPool()
    observation = _pr("pr-1", delivery_id=None)
    await insert_pull_request_observation(pool, observation)
    payload = json.loads(pool.conn.pr[0]["payload"])
    payload["legacy_status"] = "passed"
    pool.conn.pr[0]["payload"] = payload

    with pytest.raises(ObservationDecodeError, match="strict schema"):
        await list_pull_request_observations(pool, "cs-1")


@pytest.mark.asyncio
async def test_remediation_attempts_are_append_only_and_exact_head_scoped():
    pool = _ObservationPool()
    first = _attempt("attempt-1")
    second = _attempt(
        "attempt-1",
        event_sequence=2,
        started_at=_T0 + timedelta(minutes=5),
        final=True,
    )
    other_head = _attempt("attempt-3", head_sha="head-b")

    assert await insert_ci_remediation_attempt(pool, first) is True
    assert await insert_ci_remediation_attempt(pool, first) is False
    assert await insert_ci_remediation_attempt(pool, second) is True
    assert await insert_ci_remediation_attempt(pool, other_head) is True

    assert await list_ci_remediation_attempts(
        pool, "cs-1", failed_head_sha="head-a", limit=1
    ) == [second]
    assert await latest_ci_remediation_attempt(
        pool, "cs-1", failed_head_sha="head-b"
    ) == other_head
    assert pool.conn.remediation[0]["payload"] == first.model_dump_json()


@pytest.mark.asyncio
async def test_sql_contract_contains_predicates_and_no_overwrite_path():
    pool = _ObservationPool()
    await insert_pull_request_observation(
        pool, _pr("pr-open", delivery_id="delivery-open")
    )
    await insert_ci_verification_observation(pool, _ci("ci-failed", failed=True))
    await insert_ci_remediation_attempt(pool, _attempt("attempt-1"))

    inserts = [query for query in pool.conn.queries if "INSERT INTO" in query]
    assert inserts
    assert all("ON CONFLICT" in query for query in inserts)
    assert all("DO UPDATE" not in query and "UPDATE " not in query for query in inserts)
