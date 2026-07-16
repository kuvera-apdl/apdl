"""In-memory PostgreSQL fakes for codegen endpoint and job tests.

The fake mirrors the canonical Phase-8 split between changeset lifecycle,
GitHub pull-request state, exact-head external CI, and immutable remediation
journals. Query handling intentionally follows the store modules' SQL shapes;
it does not preserve the removed CI-as-lifecycle columns or statuses.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from app.safety.policy import TenantCodegenConnectionPolicy

_T0 = datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc)


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _latest(rows: list[dict[str, Any]], *keys: str) -> dict[str, Any] | None:
    if not rows:
        return None
    return max(rows, key=lambda row: tuple(row.get(key) or "" for key in keys))


class _Txn:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class FakeConn:
    def __init__(self, store: dict[str, Any]) -> None:
        self.store = store

    def transaction(self) -> _Txn:
        return _Txn()

    def _rows(self, name: str) -> dict[Any, dict[str, Any]]:
        return self.store.setdefault(name, {})

    def _connected_changeset(self, changeset_id: str) -> dict[str, Any] | None:
        row = self._rows("changesets").get(changeset_id)
        if row is None:
            return None
        return {
            **row,
            "connected_repository": row.get("repository_full_name"),
        }

    def _insert_pr_observation(self, args: tuple[Any, ...]) -> str | None:
        rows = self._rows("pull_request_observations")
        observation_id, delivery_id = args[0], args[1]
        duplicate_delivery = delivery_id is not None and any(
            row["delivery_id"] == delivery_id for row in rows.values()
        )
        if observation_id in rows or duplicate_delivery:
            return None
        rows[observation_id] = {
            "observation_id": observation_id,
            "delivery_id": delivery_id,
            "changeset_id": args[2],
            "repository": args[3],
            "pr_number": args[4],
            "head_sha": args[5],
            "status": args[6],
            "github_updated_at": args[7],
            "observed_at": args[8],
            "payload": args[9],
        }
        return observation_id

    def _insert_ci_observation(self, args: tuple[Any, ...]) -> str | None:
        rows = self._rows("ci_verification_observations")
        observation_id = args[0]
        duplicate_evidence = any(
            row["changeset_id"] == args[1]
            and row["head_sha"] == args[4]
            and row["evidence_hash"] == args[6]
            for row in rows.values()
        )
        if observation_id in rows or duplicate_evidence:
            return None
        rows[observation_id] = {
            "observation_id": observation_id,
            "changeset_id": args[1],
            "repository": args[2],
            "pr_number": args[3],
            "head_sha": args[4],
            "status": args[5],
            "evidence_hash": args[6],
            "observed_at": args[7],
            "payload": args[8],
        }
        return observation_id

    def _insert_runtime_observation(self, args: tuple[Any, ...]) -> str | None:
        rows = self._rows("runtime_evidence_observations")
        observation_id = args[0]
        duplicate_evidence = any(
            row["changeset_id"] == args[1]
            and row["head_sha"] == args[4]
            and row["evidence_hash"] == args[6]
            for row in rows.values()
        )
        if observation_id in rows or duplicate_evidence:
            return None
        rows[observation_id] = {
            "observation_id": observation_id,
            "changeset_id": args[1],
            "repository": args[2],
            "pr_number": args[3],
            "head_sha": args[4],
            "ci_observation_id": args[5],
            "evidence_hash": args[6],
            "observed_at": args[7],
            "payload": args[8],
        }
        return observation_id

    def _insert_remediation_attempt(self, args: tuple[Any, ...]) -> str | None:
        rows = self._rows("ci_remediation_attempts")
        event_id = args[0]
        duplicate_sequence = any(
            row["attempt_id"] == args[1] and row["event_sequence"] == args[2]
            for row in rows.values()
        )
        if event_id in rows or duplicate_sequence:
            return None
        rows[event_id] = {
            "event_id": event_id,
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
        return event_id

    def _failed_observation_is_current(self, args: tuple[Any, ...]) -> bool:
        changeset_id, failed_head, scope, observation_id = args
        ci_rows = [
            row
            for row in self._rows("ci_verification_observations").values()
            if row["changeset_id"] == changeset_id and row["head_sha"] == failed_head
        ]
        latest_ci = _latest(ci_rows, "observed_at", "observation_id")
        if (
            latest_ci is None
            or latest_ci["observation_id"] != observation_id
            or latest_ci["status"] != "failed"
        ):
            return False
        signals = (_json(latest_ci["payload"]) or {}).get("signals", [])
        if scope.startswith("check_suite:"):
            identity = scope.partition(":")[2]
            return any(
                str(signal.get("check_suite_id")) == identity
                and signal.get("conclusion") == "failed"
                for signal in signals
            )
        return any(
            signal.get("signal_id") == scope and signal.get("conclusion") == "failed"
            for signal in signals
        )

    async def execute(self, query: str, *args: Any) -> None:
        if "SELECT pg_notify" in query:
            self.store.setdefault("grant_notifications", []).append(
                {"channel": args[0], "grant_id": args[1]}
            )
            return None
        if "UPDATE github_repository_grants" in query:
            project_id = args[0]
            for grant in self._rows("repository_grants").values():
                if grant["project_id"] == project_id and grant["status"] == "active":
                    grant["status"] = "revoked"
                    grant["revoked_at"] = _T0
                    grant["updated_at"] = _T0
            return None
        if "DELETE FROM codegen_runtime_collection_claims" in query:
            self._rows("runtime_collection_claims").pop(
                (args[0], args[1], args[2]), None
            )
            return None
        row = self._rows("changesets").get(args[0]) if args else None
        if row is None:
            return None
        if "SET prompts" in query:
            row["prompts"] = args[1]
        elif "SET contract_bundle" in query:
            row["contract_bundle"] = args[1]
        elif "SET requirement_ledger" in query:
            row["requirement_ledger"] = args[1]
        elif "SET inspection_snapshot" in query:
            if args[1] is not None:
                row["inspection_snapshot"] = args[1]
            if args[2] is not None:
                row["dependency_slice"] = args[2]
        elif "SET verification_plan" in query:
            if args[1] is not None:
                row["verification_plan"] = args[1]
            if args[2] is not None:
                row["verification_coverage"] = args[2]
        elif "SET review_verdict" in query:
            row["review_verdict"] = args[1]
        elif "SET runtime_acceptance_plan" in query:
            row["runtime_acceptance_plan"] = args[1]
        elif "SET publication_authorization" in query:
            row["publication_authorization"] = args[1]
        row["updated_at"] = _T0
        return None

    async def fetchval(self, query: str, *args: Any) -> Any:
        if "SELECT 1" in query and "FROM" not in query:
            return 1
        if "SELECT status FROM codegen_changesets" in query:
            row = self._rows("changesets").get(args[0])
            return row["status"] if row else None
        if "SELECT project_id" in query and "retry_of_changeset_id = $1" in query:
            row = next(
                (
                    item
                    for item in self._rows("changesets").values()
                    if item.get("retry_of_changeset_id") == args[0]
                ),
                None,
            )
            return row["project_id"] if row else None
        if "SELECT COALESCE(MIN(started_at), $2)" in query:
            values = [
                row["started_at"]
                for row in self._rows("ci_remediation_attempts").values()
                if row["changeset_id"] == args[0]
            ]
            return min(values) if values else args[1]
        if "SELECT now() > $1" in query:
            observed_at, seconds = args
            return datetime.now(timezone.utc) > observed_at + timedelta(seconds=seconds)
        if "INSERT INTO codegen_pull_request_observations" in query:
            return self._insert_pr_observation(args)
        if "SELECT observation_id FROM codegen_pull_request_observations" in query:
            values = [
                row
                for row in self._rows("pull_request_observations").values()
                if row["changeset_id"] == args[0]
            ]
            latest = _latest(
                values, "github_updated_at", "observed_at", "observation_id"
            )
            return latest["observation_id"] if latest else None
        if "INSERT INTO codegen_ci_verification_observations" in query:
            return self._insert_ci_observation(args)
        if "SELECT observation_id FROM codegen_ci_verification_observations" in query:
            values = [
                row
                for row in self._rows("ci_verification_observations").values()
                if row["changeset_id"] == args[0] and row["head_sha"] == args[1]
            ]
            latest = _latest(values, "observed_at", "observation_id")
            return latest["observation_id"] if latest else None
        if "INSERT INTO codegen_runtime_evidence_observations" in query:
            return self._insert_runtime_observation(args)
        if "INSERT INTO codegen_runtime_collection_claims" in query:
            key = (args[0], args[1], args[2])
            if any(
                row["changeset_id"] == args[0]
                and row["head_sha"] == args[1]
                and row["ci_observation_id"] == args[2]
                for row in self._rows("runtime_evidence_observations").values()
            ):
                return None
            claims = self._rows("runtime_collection_claims")
            if key in claims:
                return None
            claims[key] = {"claimed_at": datetime.now(timezone.utc)}
            return args[2]
        if "SELECT observation_id FROM codegen_runtime_evidence_observations" in query:
            values = [
                row
                for row in self._rows("runtime_evidence_observations").values()
                if row["changeset_id"] == args[0] and row["head_sha"] == args[1]
            ]
            latest = _latest(values, "observed_at", "observation_id")
            return latest["observation_id"] if latest else None
        if "INSERT INTO codegen_ci_remediation_attempts" in query:
            return self._insert_remediation_attempt(args)
        if "INSERT INTO codegen_ci_remediation_claims" in query:
            if (
                "SELECT $1, $2, $3, $4" in query
                and not self._failed_observation_is_current(args)
            ):
                return None
            changeset_id, failed_head = args[0], args[1]
            scope, observation_id = args[2], args[3]
            key = (changeset_id, failed_head, scope)
            claims = self._rows("ci_remediation_claims")
            if key in claims:
                return None
            claims[key] = {
                "changeset_id": changeset_id,
                "failed_head_sha": failed_head,
                "claim_scope": scope,
                "failure_observation_id": observation_id,
                "claimed_at": datetime.now(timezone.utc),
            }
            return changeset_id
        raise AssertionError(f"Unexpected fetchval: {query}")

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if "INSERT INTO github_repository_grants" in query:
            row = {
                "grant_id": args[0],
                "project_id": args[1],
                "installation_id": args[2],
                "repository_id": args[3],
                "repository_full_name": args[4],
                "status": "active",
                "authorization_source": "operator",
                "authorization_subject": args[5],
                "verified_at": _T0,
                "revoked_at": None,
                "created_at": _T0,
                "updated_at": _T0,
            }
            self._rows("repository_grants")[args[0]] = row
            return row
        if "UPDATE github_repository_grants" in query:
            project_id, grant_id = args
            row = self._rows("repository_grants").get(grant_id)
            if (
                row is None
                or row["project_id"] != project_id
                or row["status"] != "active"
            ):
                return None
            row["status"] = "revoked"
            row["revoked_at"] = _T0
            row["updated_at"] = _T0
            return {"grant_id": grant_id}
        if "FROM github_repository_grants" in query and "SELECT *" in query:
            project_id, grant_id = args
            row = self._rows("repository_grants").get(grant_id)
            if row is None or row["project_id"] != project_id:
                return None
            if "status = 'active'" in query and row["status"] != "active":
                return None
            return row
        if "INSERT INTO codegen_connections" in query:
            project_id, grant_id, branch, tenant_policy = args
            grant = self._rows("repository_grants").get(grant_id)
            if (
                grant is None
                or grant["project_id"] != project_id
                or grant["status"] != "active"
            ):
                return None
            row = self._rows("connections").get(args[0])
            if row is None:
                row = {
                    "project_id": project_id,
                    "grant_id": grant_id,
                    "default_base_branch": branch,
                    "tenant_policy": tenant_policy,
                    "created_at": _T0,
                    "updated_at": _T0,
                }
            else:
                row.update(
                    grant_id=grant_id,
                    default_base_branch=branch,
                    updated_at=_T0,
                )
            self._rows("connections")[project_id] = row
            return {"project_id": project_id}
        if "UPDATE codegen_connections" in query and "SET tenant_policy" in query:
            row = self._rows("connections").get(args[0])
            grant = (
                self._rows("repository_grants").get(row["grant_id"])
                if row is not None
                else None
            )
            if row is None or grant is None or grant["status"] != "active":
                return None
            row["tenant_policy"] = args[1]
            row["updated_at"] = _T0
            return {"tenant_policy": row["tenant_policy"]}
        if "SELECT tenant_policy" in query and "FROM codegen_connections" in query:
            row = self._rows("connections").get(args[0])
            grant = (
                self._rows("repository_grants").get(row["grant_id"])
                if row is not None
                else None
            )
            return (
                {"tenant_policy": row["tenant_policy"]}
                if row is not None and grant is not None and grant["status"] == "active"
                else None
            )
        if (
            "FROM codegen_connections AS connection" in query
            and "JOIN github_repository_grants AS grant_record" in query
        ):
            connection = self._rows("connections").get(args[0])
            grant = (
                self._rows("repository_grants").get(connection["grant_id"])
                if connection is not None
                else None
            )
            if grant is None or grant["status"] != "active":
                return None
            return {
                **connection,
                "installation_id": grant["installation_id"],
                "repository_id": grant["repository_id"],
                "repository_full_name": grant["repository_full_name"],
            }
        if "SELECT * FROM codegen_connections" in query:
            return self._rows("connections").get(args[0])
        if "DELETE FROM codegen_connections" in query:
            return self._rows("connections").pop(args[0], None)
        if (
            "FROM codegen_changesets AS changeset" in query
            and "JOIN github_repository_grants AS grant_record" in query
        ):
            changeset = self._rows("changesets").get(args[0])
            if changeset is None or changeset.get("repository_target_quarantined"):
                return None
            grant = self._rows("repository_grants").get(
                changeset.get("repository_grant_id")
            )
            if (
                grant is None
                or grant["project_id"] != changeset["project_id"]
                or grant["status"] != "active"
                or grant["repository_id"] != changeset.get("repository_id")
                or grant["installation_id"]
                != changeset.get("repository_installation_id")
                or changeset.get("base_branch") is None
                or changeset.get("tenant_policy_snapshot") is None
            ):
                return None
            return {
                "project_id": changeset["project_id"],
                "grant_id": changeset["repository_grant_id"],
                "installation_id": changeset["repository_installation_id"],
                "repository_id": changeset["repository_id"],
                "repository_full_name": changeset["repository_full_name"],
                "default_base_branch": changeset["base_branch"],
                "tenant_policy": changeset["tenant_policy_snapshot"],
                "created_at": changeset["created_at"],
                "updated_at": changeset["updated_at"],
            }
        if (
            "SELECT *" in query
            and "FROM codegen_changesets" in query
            and "WHERE changeset_id = $1" in query
        ):
            return self._rows("changesets").get(args[0])
        if "INSERT INTO codegen_changesets" in query:
            is_retry = "retry_of_changeset_id" in query
            retry_of_changeset_id = args[14] if is_retry else None
            control_metadata = args[15] if is_retry else args[14]
            if any(
                (
                    is_retry
                    and existing.get("retry_of_changeset_id") == retry_of_changeset_id
                )
                or (
                    existing["project_id"] == args[1]
                    and existing.get("idempotency_key") == args[2]
                )
                for existing in self._rows("changesets").values()
            ):
                return None
            row = {
                "changeset_id": args[0],
                "project_id": args[1],
                "idempotency_key": args[2],
                "idempotency_request_sha256": args[3],
                "repository_grant_id": args[10],
                "repository_id": args[11],
                "repository_installation_id": args[12],
                "repository_full_name": args[13],
                "repository_target_quarantined": False,
                "run_id": args[4],
                "status": args[5],
                "base_branch": args[6],
                "branch": None,
                "pr_url": None,
                "pr_number": None,
                "head_sha": None,
                "github_pr_status": None,
                "external_ci_status": None,
                "external_ci_awaiting_since": None,
                "ci_retry_count": 0,
                "ci_remediation_status": "idle",
                "ci_failure_key": None,
                "ci_failure_summary": None,
                "merge_sha": None,
                "task": args[7],
                "diff_stat": "{}",
                "prompts": "[]",
                "contract_bundle": None,
                "requirement_ledger": None,
                "inspection_snapshot": None,
                "dependency_slice": None,
                "verification_plan": None,
                "verification_coverage": None,
                "review_verdict": None,
                "runtime_acceptance_plan": None,
                "runtime_evidence_assessment": None,
                "publication_authorization": None,
                "tenant_policy_snapshot": args[8],
                "effective_safety_policy_sha256": args[9],
                "retry_of_changeset_id": retry_of_changeset_id,
                "control_metadata": control_metadata,
                "error": None,
                "created_at": _T0,
                "updated_at": _T0,
            }
            self._rows("changesets")[args[0]] = row
            return row

        if "WHERE project_id = $1 AND idempotency_key = $2" in query:
            return next(
                (
                    row
                    for row in self._rows("changesets").values()
                    if row["project_id"] == args[0]
                    and row.get("idempotency_key") == args[1]
                ),
                None,
            )

        if "WHERE project_id = $1 AND retry_of_changeset_id = $2" in query:
            return next(
                (
                    row
                    for row in self._rows("changesets").values()
                    if row["project_id"] == args[0]
                    and row.get("retry_of_changeset_id") == args[1]
                ),
                None,
            )

        if "WHERE retry_of_changeset_id = $1" in query:
            return next(
                (
                    row
                    for row in self._rows("changesets").values()
                    if row.get("retry_of_changeset_id") == args[0]
                ),
                None,
            )

        if "SELECT cs.*, cs.repository_full_name AS connected_repository" in query:
            return self._connected_changeset(args[0])
        if "cs.head_sha = $1" in query and "cs.repository_id = $2" in query:
            head_sha, repository_id, installation_id = args
            values = [
                row
                for row in self._rows("changesets").values()
                if row.get("head_sha") == head_sha
                and row["status"] == "pr_open"
                and row.get("repository_id") == repository_id
                and row.get("repository_installation_id") == installation_id
                and not row.get("repository_target_quarantined")
            ]
            values.sort(key=lambda row: row["created_at"], reverse=True)
            return values[0] if values else None
        if "cs.pr_number = $1" in query and "cs.repository_id = $2" in query:
            pr_number, repository_id, installation_id = args
            values = [
                row
                for row in self._rows("changesets").values()
                if row.get("pr_number") == pr_number
                and row.get("repository_id") == repository_id
                and row.get("repository_installation_id") == installation_id
                and not row.get("repository_target_quarantined")
            ]
            values.sort(key=lambda row: row["created_at"], reverse=True)
            return values[0] if values else None
        if "SELECT payload FROM codegen_ci_remediation_attempts" in query:
            changeset_id, resulting_sha = args
            values = []
            for row in self._rows("ci_remediation_attempts").values():
                payload = _json(row["payload"])
                if (
                    row["changeset_id"] == changeset_id
                    and payload.get("resulting_commit_sha") == resulting_sha
                    and payload.get("disposition") == "awaiting_ci"
                ):
                    values.append(row)
            latest = _latest(values, "recorded_at", "event_sequence")
            return {"payload": latest["payload"]} if latest else None
        if "SELECT * FROM codegen_changesets" in query:
            return self._rows("changesets").get(args[0])

        if "UPDATE codegen_changesets" in query:
            row = self._rows("changesets").get(args[0])
            if row is None:
                return None

            if "SET tenant_policy_snapshot = COALESCE" in query:
                if row.get("tenant_policy_snapshot") is None:
                    row["tenant_policy_snapshot"] = args[1]
                row["effective_safety_policy_sha256"] = args[2]
            elif "SET status = 'pr_open', branch = $2" in query:
                row.update(
                    status="pr_open",
                    branch=args[1],
                    pr_url=args[2],
                    pr_number=args[3],
                    head_sha=args[4],
                    github_pr_status=args[5],
                    external_ci_status=args[6],
                    diff_stat=args[7],
                    external_ci_awaiting_since=datetime.now(timezone.utc),
                    ci_remediation_status="idle",
                )
            elif "SET status = $2, head_sha = $3, github_pr_status = $4" in query:
                row["status"] = args[1]
                row["head_sha"] = args[2]
                row["github_pr_status"] = args[3]
                if args[3] == "merged":
                    row["merge_sha"] = args[4]
                if args[5]:
                    row["external_ci_status"] = "pending"
                    row["external_ci_awaiting_since"] = datetime.now(timezone.utc)
                    row["ci_remediation_status"] = "idle"
                    row["ci_failure_key"] = None
                    row["ci_failure_summary"] = None
                    row["runtime_evidence_assessment"] = None
            elif "SET external_ci_status = $2, ci_remediation_status = $3" in query:
                if row.get("head_sha") != args[5]:
                    return None
                row["external_ci_status"] = args[1]
                row["ci_remediation_status"] = args[2]
                row["ci_failure_key"] = args[3]
                row["ci_failure_summary"] = args[4]
                row["runtime_evidence_assessment"] = None
            elif "SET runtime_evidence_assessment = $2::jsonb" in query:
                if row.get("head_sha") != args[2]:
                    return None
                row["runtime_evidence_assessment"] = args[1]
            elif (
                "SET ci_retry_count = $2, ci_remediation_status = 'diagnosing'" in query
            ):
                row["ci_retry_count"] = args[1]
                row["ci_remediation_status"] = "diagnosing"
                row["ci_failure_key"] = args[2]
                row["ci_failure_summary"] = args[3]
            elif "SET ci_remediation_status = 'exhausted'" in query:
                row["ci_remediation_status"] = "exhausted"
            elif "SET head_sha = $2, external_ci_status = 'pending'" in query:
                row["head_sha"] = args[1]
                if len(args) > 2 and args[2] is not None:
                    row["runtime_acceptance_plan"] = args[2]
                row["external_ci_status"] = "pending"
                row["external_ci_awaiting_since"] = datetime.now(timezone.utc)
                row["ci_remediation_status"] = "awaiting_ci"
                row["ci_failure_key"] = None
                row["ci_failure_summary"] = None
                row["runtime_evidence_assessment"] = None
                row["error"] = None
            elif "SET ci_remediation_status = $3" in query:
                if (
                    row.get("head_sha") != args[1]
                    or row["status"] != "pr_open"
                    or row.get("github_pr_status") not in {"open", "draft"}
                ):
                    return None
                row["ci_remediation_status"] = args[2]
            elif "SET ci_remediation_status = $2" in query:
                row["ci_remediation_status"] = args[1]
                if args[2] is not None:
                    row["error"] = args[2]
            elif "merge_sha = $3" in query:
                row["status"] = args[1]
                row["merge_sha"] = args[2]
            else:
                row["status"] = args[1]
                if len(args) > 2 and args[2] is not None:
                    row["error"] = args[2]
            row["updated_at"] = _T0
            return row
        raise AssertionError(f"Unexpected fetchrow: {query}")

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "UPDATE github_repository_grants" in query:
            project_id = args[0]
            revoked = []
            for grant in self._rows("repository_grants").values():
                if grant["project_id"] == project_id and grant["status"] == "active":
                    grant["status"] = "revoked"
                    grant["revoked_at"] = _T0
                    grant["updated_at"] = _T0
                    revoked.append({"grant_id": grant["grant_id"]})
            return revoked
        if "FROM codegen_pull_request_observations" in query:
            changeset_id, head_sha, limit = args
            rows = [
                row
                for row in self._rows("pull_request_observations").values()
                if row["changeset_id"] == changeset_id
                and (head_sha is None or row["head_sha"] == head_sha)
            ]
            rows.sort(
                key=lambda row: (
                    row["github_updated_at"],
                    row["observed_at"],
                    row["observation_id"],
                ),
                reverse=True,
            )
            return [{"payload": row["payload"]} for row in rows[:limit]]
        if "FROM codegen_ci_verification_observations" in query:
            changeset_id, head_sha, limit = args
            rows = [
                row
                for row in self._rows("ci_verification_observations").values()
                if row["changeset_id"] == changeset_id
                and (head_sha is None or row["head_sha"] == head_sha)
            ]
            rows.sort(
                key=lambda row: (row["observed_at"], row["observation_id"]),
                reverse=True,
            )
            return [{"payload": row["payload"]} for row in rows[:limit]]
        if "FROM codegen_runtime_evidence_observations" in query:
            changeset_id, head_sha, ci_observation_id, limit = args
            rows = [
                row
                for row in self._rows("runtime_evidence_observations").values()
                if row["changeset_id"] == changeset_id
                and (head_sha is None or row["head_sha"] == head_sha)
                and (
                    ci_observation_id is None
                    or row["ci_observation_id"] == ci_observation_id
                )
            ]
            rows.sort(
                key=lambda row: (row["observed_at"], row["observation_id"]),
                reverse=True,
            )
            return [{"payload": row["payload"]} for row in rows[:limit]]
        if "FROM codegen_ci_remediation_attempts" in query:
            changeset_id, failed_head, limit = args
            rows = [
                row
                for row in self._rows("ci_remediation_attempts").values()
                if row["changeset_id"] == changeset_id
                and (failed_head is None or row["failed_head_sha"] == failed_head)
            ]
            rows.sort(
                key=lambda row: (
                    row["recorded_at"],
                    row["attempt_number"],
                    row["event_sequence"],
                    row["event_id"],
                ),
                reverse=True,
            )
            return [{"payload": row["payload"]} for row in rows[:limit]]
        if "UPDATE codegen_changesets" in query and "status = ANY" in query:
            transient = set(args[1])
            swept = []
            for row in self._rows("changesets").values():
                if row["status"] in transient:
                    row["status"] = "error"
                    row["error"] = row.get("error") or args[0]
                    row["updated_at"] = _T0
                    swept.append({"changeset_id": row["changeset_id"]})
            return swept
        if (
            "SELECT changeset_id FROM codegen_changesets" in query
            and "status = ANY" in query
        ):
            wanted = set(args[0])
            rows = [
                {"changeset_id": row["changeset_id"]}
                for row in self._rows("changesets").values()
                if row["status"] in wanted
                and (
                    "github_pr_status" not in query
                    or row.get("github_pr_status") in {None, "open", "draft"}
                )
            ]
            rows.sort(key=lambda row: row["changeset_id"])
            return rows
        if "FROM codegen_changesets" in query and "WHERE project_id" in query:
            rows = [
                row
                for row in self._rows("changesets").values()
                if row["project_id"] == args[0]
            ]
            rows.sort(key=lambda row: row["created_at"], reverse=True)
            return rows[: args[1]]
        raise AssertionError(f"Unexpected fetch: {query}")


class _Acquire:
    def __init__(self, conn: FakeConn) -> None:
        self.conn = conn

    async def __aenter__(self) -> FakeConn:
        return self.conn

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class FakePool:
    def __init__(self, store: dict[str, Any] | None = None) -> None:
        self.store = store if store is not None else {}
        for name in (
            "repository_grants",
            "connections",
            "changesets",
            "pull_request_observations",
            "ci_verification_observations",
            "runtime_evidence_observations",
            "runtime_collection_claims",
            "ci_remediation_attempts",
            "ci_remediation_claims",
        ):
            self.store.setdefault(name, {})
        self.store.setdefault("grant_notifications", [])
        self.conn = FakeConn(self.store)

    def acquire(self) -> _Acquire:
        return _Acquire(self.conn)

    def add_connection(
        self,
        project_id: str,
        repo: str = "acme/widgets",
        installation_id: int = 1,
        repository_id: int = 10,
        grant_id: str | None = None,
        tenant_policy: str
        | dict[str, Any]
        | TenantCodegenConnectionPolicy
        | None = None,
    ) -> None:
        """Seed a repo connection so changeset creation is permitted."""
        if tenant_policy is None:
            stored_policy: str | dict[str, Any] = json.dumps(
                TenantCodegenConnectionPolicy().model_dump(mode="json")
            )
        elif isinstance(tenant_policy, TenantCodegenConnectionPolicy):
            stored_policy = json.dumps(tenant_policy.model_dump(mode="json"))
        else:
            stored_policy = tenant_policy
        active_grant_id = grant_id or f"ghg_{project_id}repository"
        self.store["repository_grants"][active_grant_id] = {
            "grant_id": active_grant_id,
            "project_id": project_id,
            "installation_id": installation_id,
            "repository_id": repository_id,
            "repository_full_name": repo,
            "status": "active",
            "authorization_source": "operator",
            "authorization_subject": "test-operator",
            "verified_at": _T0,
            "revoked_at": None,
            "created_at": _T0,
            "updated_at": _T0,
        }
        self.store["connections"][project_id] = {
            "project_id": project_id,
            "grant_id": active_grant_id,
            "default_base_branch": "main",
            "tenant_policy": stored_policy,
            "created_at": _T0,
            "updated_at": _T0,
        }

    def add_changeset(
        self,
        changeset_id: str,
        project_id: str = "demo",
        *,
        status: str = "queued",
        external_ci_status: str | None = None,
        external_ci_awaiting_since: datetime | None = None,
        pr_number: int | None = None,
        head_sha: str | None = None,
        github_pr_status: str | None = None,
        branch: str | None = None,
        base_branch: str = "main",
        merge_sha: str | None = None,
        control_metadata: dict[str, Any] | None = None,
    ) -> None:
        """Seed one canonical lifecycle row for endpoint and job tests."""
        connection = self.store["connections"].get(project_id)
        grant = (
            self.store["repository_grants"].get(connection["grant_id"])
            if connection is not None
            else None
        )
        self.store["changesets"][changeset_id] = {
            "changeset_id": changeset_id,
            "project_id": project_id,
            "idempotency_key": f"test:{changeset_id}",
            "idempotency_request_sha256": "0" * 64,
            "repository_grant_id": grant["grant_id"] if grant else None,
            "repository_id": grant["repository_id"] if grant else None,
            "repository_installation_id": (grant["installation_id"] if grant else None),
            "repository_full_name": grant["repository_full_name"] if grant else None,
            "repository_target_quarantined": grant is None,
            "run_id": None,
            "status": status,
            "base_branch": base_branch,
            "branch": branch,
            "pr_url": (
                f"https://github.com/acme/widgets/pull/{pr_number}"
                if pr_number
                else None
            ),
            "pr_number": pr_number,
            "head_sha": head_sha,
            "github_pr_status": github_pr_status,
            "external_ci_status": external_ci_status,
            "external_ci_awaiting_since": external_ci_awaiting_since,
            "ci_retry_count": 0,
            "ci_remediation_status": "idle",
            "ci_failure_key": None,
            "ci_failure_summary": None,
            "merge_sha": merge_sha,
            "task": json.dumps(
                {
                    "title": "t",
                    "spec": "spec spec spec",
                    "context": {},
                    "constraints": [],
                }
            ),
            "diff_stat": "{}",
            "prompts": "[]",
            "contract_bundle": None,
            "requirement_ledger": None,
            "inspection_snapshot": None,
            "dependency_slice": None,
            "verification_plan": None,
            "verification_coverage": None,
            "review_verdict": None,
            "runtime_acceptance_plan": None,
            "runtime_evidence_assessment": None,
            "publication_authorization": None,
            "tenant_policy_snapshot": (
                connection["tenant_policy"] if connection is not None else None
            ),
            "effective_safety_policy_sha256": None,
            "retry_of_changeset_id": None,
            "control_metadata": json.dumps(
                control_metadata
                or {
                    "schema_version": "changeset_controls@1",
                    "risk_level": "high",
                    "revert": None,
                }
            ),
            "error": None,
            "created_at": _T0,
            "updated_at": _T0,
        }
