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
from uuid import NAMESPACE_URL, UUID, uuid5

from app.config import (
    codegen_revision,
    codegen_rollout_stage,
)
from app.editor.environment import codegen_tenant_behavior_configuration_sha256
from app.llm.provider_catalog import CATALOG_VERSION, catalog_model
from app.safety.policy import TenantCodegenConnectionPolicy

_T0 = datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc)
_UNSET = object()


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
        self._connection_id = object()
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True
        locks = self.store.setdefault("advisory_locks", {})
        for lock_key, owner in list(locks.items()):
            if owner is self._connection_id:
                del locks[lock_key]

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

    def _active_llm_assignment_rows(
        self,
        project_id: str,
        *,
        include_model_metadata: bool,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for role in ("editor", "helper"):
            assignment = self._rows("llm_assignments").get((project_id, role))
            if assignment is None:
                continue
            provider = assignment["provider"]
            connection = self._rows("llm_connections").get(
                (project_id, provider)
            )
            if (
                connection is None
                or connection["state"] != "active"
                or connection["version"] != assignment["connection_version"]
                or connection["inventory_version"]
                != assignment["inventory_version"]
                or connection["catalog_version"]
                != assignment["catalog_version"]
            ):
                continue
            credential = self._rows("llm_credentials").get(
                connection["credential_id"]
            )
            if (
                credential is None
                or credential["project_id"] != project_id
                or credential["provider"] != provider
                or credential["state"] != "active"
            ):
                continue
            if (credential["connection_id"], "codegen") not in self._rows(
                "llm_connection_consumers"
            ):
                continue
            model = self._rows("llm_models").get(
                (project_id, provider, assignment["model_id"])
            )
            if (
                model is None
                or model["connection_version"] != connection["version"]
                or model["inventory_version"]
                != connection["inventory_version"]
                or role not in model["supported_roles"]
            ):
                continue
            row = {
                "role": role,
                "provider": provider,
                "model_id": assignment["model_id"],
                "connection_state": "active",
            }
            if include_model_metadata:
                row.update(
                    assignment_version=assignment["assignment_version"],
                    connection_version=assignment["connection_version"],
                    inventory_version=assignment["inventory_version"],
                    catalog_version=assignment["catalog_version"],
                    context_window_tokens=model["context_window_tokens"],
                    supports_tool_calling=model["supports_tool_calling"],
                    supports_structured_output=model[
                        "supports_structured_output"
                    ],
                    input_cost_per_million_tokens_usd_micros=model[
                        "input_cost_per_million_tokens_usd_micros"
                    ],
                    output_cost_per_million_tokens_usd_micros=model[
                        "output_cost_per_million_tokens_usd_micros"
                    ],
                )
            rows.append(row)
        return rows

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

    def _insert_pr_publication_event(self, args: tuple[Any, ...]) -> str | None:
        rows = self._rows("pull_request_publication_events")
        event_id = args[0]
        if event_id in rows:
            return None
        payload = _json(args[8])
        if payload.get("event_type") == "intent_recorded" and any(
            _json(row["payload"]).get("event_type") == "intent_recorded"
            and row["changeset_id"] == args[1]
            for row in rows.values()
        ):
            return None
        rows[event_id] = {
            "event_id": event_id,
            "event_sequence": len(rows) + 1,
            "changeset_id": args[1],
            "event_type": args[2],
            "intent_event_id": args[3],
            "cleanup_request_event_id": args[4],
            "pr_number": args[5],
            "github_url": args[6],
            "recorded_at": args[7],
            "payload": args[8],
        }
        return event_id

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
        if "DELETE FROM github_repository_authorization_flows AS flow" in query:
            flows = self._rows("repository_authorization_flows")
            expired = sorted(
                (
                    row
                    for row in flows.values()
                    if row["expires_at"] <= datetime.now(timezone.utc)
                ),
                key=lambda row: row["expires_at"],
            )[:100]
            authorization_ids = {row["authorization_id"] for row in expired}
            for authorization_id in authorization_ids:
                flows.pop(authorization_id, None)
            candidates = self._rows("repository_authorization_candidates")
            for candidate_id, candidate in list(candidates.items()):
                if candidate["authorization_id"] in authorization_ids:
                    del candidates[candidate_id]
            return None
        if "DELETE FROM github_repository_authorization_candidates" in query:
            candidates = self._rows("repository_authorization_candidates")
            if args:
                authorization_ids = {args[0]}
            else:
                authorization_ids = {
                    row["authorization_id"]
                    for row in self._rows(
                        "repository_authorization_flows"
                    ).values()
                    if row["expires_at"] <= datetime.now(timezone.utc)
                    and row["status"] != "completed"
                }
            for candidate_id, candidate in list(candidates.items()):
                if candidate["authorization_id"] in authorization_ids:
                    del candidates[candidate_id]
            return None
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
        elif "SET branch = $2, head_sha = $3" in query:
            if row["status"] == "pushing":
                row["branch"] = args[1]
                row["head_sha"] = args[2]
        elif "SET branch = $2, updated_at" in query:
            row["branch"] = args[1]
        elif "SET pr_number = COALESCE" in query:
            if row["status"] == "pushing":
                row["pr_number"] = args[1] or row.get("pr_number")
                row["pr_url"] = args[2] or row.get("pr_url")
        elif "SET status = 'error', error = $2" in query:
            row["status"] = "error"
            row["error"] = args[1]
        elif "SET status = 'error', error = COALESCE" in query:
            if row["status"] == "pushing":
                row["status"] = "error"
                row["error"] = row.get("error") or args[1]
        row["updated_at"] = _T0
        return None

    async def fetchval(self, query: str, *args: Any) -> Any:
        if "pg_try_advisory_lock" in query:
            lock_key = args[0]
            locks = self.store.setdefault("advisory_locks", {})
            owner = locks.get(lock_key)
            if owner is None:
                locks[lock_key] = self._connection_id
                return True
            return owner is self._connection_id
        if "pg_advisory_unlock(" in query:
            started = self.store.get("advisory_unlock_started")
            if started is not None:
                started.set()
            release = self.store.get("advisory_unlock_release")
            if release is not None:
                await release.wait()
            lock_key = args[0]
            locks = self.store.setdefault("advisory_locks", {})
            if locks.get(lock_key) is not self._connection_id:
                return False
            del locks[lock_key]
            return True
        if "SELECT 1" in query and "FROM" not in query:
            return 1
        if "SELECT status FROM codegen_changesets" in query:
            row = self._rows("changesets").get(args[0])
            return row["status"] if row else None
        if "SELECT llm_execution_snapshot" in query:
            row = self._rows("changesets").get(args[0])
            return row.get("llm_execution_snapshot") if row else None
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
        if "INSERT INTO codegen_pull_request_publication_events" in query:
            return self._insert_pr_publication_event(args)
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
        if "AS repository_connection_authorized" in query:
            project_id, actor_user_id = args
            project = self._rows("admin_projects").get(project_id)
            account = self._rows("admin_users").get(actor_user_id)
            membership = self._rows("admin_user_projects").get(
                (actor_user_id, project_id)
            )
            roles = set(membership["roles"]) if membership is not None else set()
            authorized = bool(
                project is not None
                and account is not None
                and account["active"]
                and (
                    project.get("owner_user_id") == actor_user_id
                    or {"agents:manage", "credentials:manage"}.issubset(roles)
                )
            )
            return (
                {"repository_connection_authorized": authorized}
                if project is not None and account is not None
                else None
            )
        if "SELECT project.owner_user_id, account.active" in query:
            project_id, actor_user_id = args
            project = self._rows("admin_projects").get(project_id)
            account = self._rows("admin_users").get(actor_user_id)
            if project is None or account is None:
                return None
            return {
                "owner_user_id": project.get("owner_user_id"),
                "active": account["active"],
            }
        if "SELECT roles" in query and "FROM admin_user_projects" in query:
            project_id, actor_user_id = args
            return self._rows("admin_user_projects").get(
                (actor_user_id, project_id)
            )
        if "INSERT INTO github_repository_authorization_flows" in query:
            authorization_id, project_id, actor_user_id, state_hash, expires_at = args
            row = {
                "authorization_id": authorization_id,
                "project_id": project_id,
                "actor_user_id": actor_user_id,
                "state_hash": state_hash,
                "status": "awaiting_installation",
                "github_user_id": None,
                "github_login": None,
                "expires_at": expires_at,
                "completed_at": None,
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
            self._rows("repository_authorization_flows")[authorization_id] = row
            return row
        if (
            "UPDATE github_repository_authorization_flows" in query
            and "WHERE state_hash = $1" in query
        ):
            state_hash, replacement_hash = args
            expected = (
                "awaiting_installation"
                if "status = 'awaiting_installation'" in query
                else "awaiting_oauth"
            )
            row = next(
                (
                    item
                    for item in self._rows(
                        "repository_authorization_flows"
                    ).values()
                    if item["state_hash"] == state_hash
                    and item["status"] == expected
                    and item["expires_at"] > datetime.now(timezone.utc)
                ),
                None,
            )
            if row is None:
                return None
            row["state_hash"] = replacement_hash
            if expected == "awaiting_installation":
                row["status"] = "awaiting_oauth"
            row["updated_at"] = datetime.now(timezone.utc)
            return row
        if (
            "DELETE FROM github_repository_authorization_flows" in query
            and "WHERE state_hash = $1" in query
            and "status = 'awaiting_installation'" in query
        ):
            (state_hash,) = args
            flows = self._rows("repository_authorization_flows")
            match = next(
                (
                    (authorization_id, row)
                    for authorization_id, row in flows.items()
                    if row["state_hash"] == state_hash
                    and row["status"] == "awaiting_installation"
                    and row["expires_at"] > datetime.now(timezone.utc)
                ),
                None,
            )
            if match is None:
                return None
            authorization_id, row = match
            del flows[authorization_id]
            return row
        if (
            "DELETE FROM github_repository_authorization_flows" in query
            and "RETURNING authorization_id" in query
        ):
            authorization_id, project_id, actor_user_id = args
            flows = self._rows("repository_authorization_flows")
            row = flows.get(authorization_id)
            if (
                row is None
                or row["project_id"] != project_id
                or row["actor_user_id"] != actor_user_id
                or row["expires_at"] > datetime.now(timezone.utc)
            ):
                return None
            del flows[authorization_id]
            candidates = self._rows("repository_authorization_candidates")
            for candidate_id, candidate in list(candidates.items()):
                if candidate["authorization_id"] == authorization_id:
                    del candidates[candidate_id]
            return {"authorization_id": authorization_id}
        if (
            "SELECT status, expires_at"
            in query
            and "FROM github_repository_authorization_flows" in query
        ):
            authorization_id, project_id, actor_user_id = args
            row = self._rows("repository_authorization_flows").get(
                authorization_id
            )
            if (
                row is None
                or row["project_id"] != project_id
                or row["actor_user_id"] != actor_user_id
            ):
                return None
            return row
        if "INSERT INTO github_repository_authorization_candidates" in query:
            (
                candidate_id,
                authorization_id,
                installation_id,
                repository_id,
                repository_full_name,
                default_base_branch,
                private,
            ) = args
            row = {
                "candidate_id": candidate_id,
                "authorization_id": authorization_id,
                "installation_id": installation_id,
                "repository_id": repository_id,
                "repository_full_name": repository_full_name,
                "default_base_branch": default_base_branch,
                "private": private,
                "created_at": datetime.now(timezone.utc),
            }
            self._rows("repository_authorization_candidates")[candidate_id] = row
            return {"candidate_id": candidate_id}
        if (
            "UPDATE github_repository_authorization_flows" in query
            and "SET status = 'awaiting_selection'" in query
        ):
            authorization_id, github_user_id, github_login = args
            row = self._rows("repository_authorization_flows").get(
                authorization_id
            )
            if row is None or row["status"] != "awaiting_oauth":
                return None
            row.update(
                status="awaiting_selection",
                github_user_id=github_user_id,
                github_login=github_login,
                updated_at=datetime.now(timezone.utc),
            )
            return {"authorization_id": authorization_id}
        if (
            "SELECT authorization_id, project_id, status, expires_at"
            in query
            and "FROM github_repository_authorization_flows" in query
        ):
            authorization_id, project_id, actor_user_id = args
            row = self._rows("repository_authorization_flows").get(
                authorization_id
            )
            if (
                row is None
                or row["project_id"] != project_id
                or row["actor_user_id"] != actor_user_id
            ):
                return None
            return row
        if (
            "FROM github_repository_authorization_flows AS flow" in query
            and "JOIN github_repository_authorization_candidates AS candidate"
            in query
        ):
            authorization_id, project_id, actor_user_id, candidate_id = args
            flow = self._rows("repository_authorization_flows").get(
                authorization_id
            )
            candidate = self._rows("repository_authorization_candidates").get(
                candidate_id
            )
            if (
                flow is None
                or candidate is None
                or flow["project_id"] != project_id
                or flow["actor_user_id"] != actor_user_id
                or candidate["authorization_id"] != authorization_id
            ):
                return None
            return {**candidate, "status": flow["status"], "expires_at": flow["expires_at"]}
        if (
            "SELECT status, github_user_id, expires_at"
            in query
            and "FROM github_repository_authorization_flows" in query
        ):
            authorization_id, project_id, actor_user_id = args
            row = self._rows("repository_authorization_flows").get(
                authorization_id
            )
            if (
                row is None
                or row["project_id"] != project_id
                or row["actor_user_id"] != actor_user_id
            ):
                return None
            return row
        if (
            "FROM github_repository_authorization_candidates" in query
            and "candidate_id = $2" in query
        ):
            if "FOR UPDATE" in query:
                raise AssertionError(
                    "Immutable repository authorization candidates must be read "
                    "without UPDATE privilege"
                )
            authorization_id, candidate_id = args
            row = self._rows("repository_authorization_candidates").get(
                candidate_id
            )
            if row is None or row["authorization_id"] != authorization_id:
                return None
            return row
        if (
            "UPDATE github_repository_authorization_flows" in query
            and "SET status = 'completed'" in query
        ):
            authorization_id = args[0]
            row = self._rows("repository_authorization_flows").get(
                authorization_id
            )
            if row is None or row["status"] != "awaiting_selection":
                return None
            row.update(
                status="completed",
                completed_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            return {"authorization_id": authorization_id}
        if "INSERT INTO github_repository_grants" in query:
            oauth = "'github_oauth'" in query
            row = {
                "grant_id": args[0],
                "project_id": args[1],
                "installation_id": args[2],
                "repository_id": args[3],
                "repository_full_name": args[4],
                "status": "active",
                "authorization_source": "github_oauth" if oauth else "operator",
                "authorization_subject": args[5],
                "authorized_by_user_id": args[6] if oauth else None,
                "github_user_id": args[7] if oauth else None,
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
            llm_execution_snapshot = args[16] if is_retry else args[15]
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
                "llm_execution_snapshot": llm_execution_snapshot,
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

        if "FROM codegen_pull_request_publication_events" in query:
            rows = [
                row
                for row in self._rows("pull_request_publication_events").values()
                if row["changeset_id"] == args[0]
            ]
            if "event_type = 'intent_recorded'" in query:
                rows = [row for row in rows if row["event_type"] == "intent_recorded"]
            elif "event_type = 'branch_published'" in query:
                rows = [row for row in rows if row["event_type"] == "branch_published"]
            if not rows:
                return None
            rows.sort(
                key=lambda row: row["event_sequence"],
                reverse=True,
            )
            return {"payload": rows[0]["payload"]}

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
        if "FROM codegen_project_model_assignments AS assignment" in query:
            return self._active_llm_assignment_rows(
                args[0],
                include_model_metadata="assignment.assignment_version" in query,
            )
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
        if "FROM github_repository_authorization_candidates" in query:
            authorization_id = args[0]
            rows = [
                row
                for row in self._rows(
                    "repository_authorization_candidates"
                ).values()
                if row["authorization_id"] == authorization_id
            ]
            rows.sort(
                key=lambda row: (
                    row["repository_full_name"].lower(),
                    row["repository_id"],
                )
            )
            return [
                {
                    "candidate_id": row["candidate_id"],
                    "repository_id": row["repository_id"],
                    "repository_full_name": row["repository_full_name"],
                    "default_base_branch": row["default_base_branch"],
                    "private": row["private"],
                }
                for row in rows
            ]
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
        if (
            "FROM codegen_pull_request_publication_events" in query
            and "SELECT payload" in query
        ):
            rows = [
                row
                for row in self._rows("pull_request_publication_events").values()
                if row["changeset_id"] == args[0]
            ]
            rows.sort(key=lambda row: row["event_sequence"])
            return [{"payload": row["payload"]} for row in rows]
        if (
            "FROM codegen_changesets AS changeset" in query
            and "event_type = 'intent_recorded'" in query
        ):
            intent_changesets = {
                row["changeset_id"]
                for row in self._rows("pull_request_publication_events").values()
                if row["event_type"] == "intent_recorded"
            }
            rows = [
                {"changeset_id": row["changeset_id"]}
                for row in self._rows("changesets").values()
                if row["status"] == "pushing"
                and row["changeset_id"] in intent_changesets
            ]
            rows.sort(key=lambda row: row["changeset_id"])
            return rows
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
            intent_changesets = {
                row["changeset_id"]
                for row in self._rows("pull_request_publication_events").values()
                if row["event_type"] == "intent_recorded"
            }
            swept = []
            for row in self._rows("changesets").values():
                if row["status"] in transient and not (
                    row["status"] == "pushing"
                    and row["changeset_id"] in intent_changesets
                ):
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
            "repository_authorization_flows",
            "repository_authorization_candidates",
            "admin_projects",
            "admin_users",
            "admin_user_projects",
            "llm_credentials",
            "llm_connections",
            "llm_connection_consumers",
            "llm_models",
            "llm_assignments",
            "llm_attempts",
            "changesets",
            "pull_request_observations",
            "pull_request_publication_events",
            "ci_verification_observations",
            "runtime_evidence_observations",
            "runtime_collection_claims",
            "ci_remediation_attempts",
            "ci_remediation_claims",
        ):
            self.store.setdefault(name, {})
        self.store.setdefault("grant_notifications", [])
        self.store.setdefault("advisory_locks", {})
        self.acquire_count = 0
        self.conn = FakeConn(self.store)

    def acquire(self) -> _Acquire:
        self.acquire_count += 1
        self.conn = FakeConn(self.store)
        return _Acquire(self.conn)

    def add_project_actor(
        self,
        project_id: str,
        actor_user_id: UUID,
        *,
        owner: bool = True,
        active: bool = True,
        roles: frozenset[str] = frozenset(
            {"agents:manage", "credentials:manage"}
        ),
    ) -> None:
        """Seed live human authority for repository-authorization tests."""
        self.store["admin_projects"][project_id] = {
            "project_id": project_id,
            "owner_user_id": actor_user_id if owner else None,
        }
        self.store["admin_users"][actor_user_id] = {
            "user_id": actor_user_id,
            "active": active,
        }
        self.store["admin_user_projects"][(actor_user_id, project_id)] = {
            "user_id": actor_user_id,
            "project_id": project_id,
            "roles": list(roles),
        }

    def add_llm_connection(
        self,
        project_id: str,
        *,
        provider: str = "anthropic",
        editor_model_id: str = "claude-sonnet-5",
        helper_model_id: str | None = None,
    ) -> None:
        """Seed one active provider inventory and both canonical assignments."""
        helper_model_id = helper_model_id or editor_model_id
        editor_model = catalog_model(provider, editor_model_id)
        helper_model = catalog_model(provider, helper_model_id)
        if editor_model is None or "editor" not in editor_model.supported_roles:
            raise ValueError("editor_model_id must be editor-eligible")
        if helper_model is None or "helper" not in helper_model.supported_roles:
            raise ValueError("helper_model_id must be helper-eligible")

        credential_id = str(
            uuid5(NAMESPACE_URL, f"apdl-test:{project_id}:{provider}")
        )
        self.store["llm_credentials"][credential_id] = {
            "credential_id": credential_id,
            "connection_id": credential_id,
            "project_id": project_id,
            "provider": provider,
            "credential_version": 1,
            "state": "active",
        }
        self.store["llm_connection_consumers"][(credential_id, "codegen")] = {
            "connection_id": credential_id,
            "consumer": "codegen",
        }
        self.store["llm_connections"][(project_id, provider)] = {
            "project_id": project_id,
            "provider": provider,
            "version": 1,
            "inventory_version": 1,
            "state": "active",
            "credential_id": credential_id,
            "catalog_version": CATALOG_VERSION,
        }
        for model in {editor_model.model_id: editor_model, helper_model.model_id: helper_model}.values():
            self.store["llm_models"][
                (project_id, provider, model.model_id)
            ] = {
                "project_id": project_id,
                "provider": provider,
                "model_id": model.model_id,
                "connection_version": 1,
                "inventory_version": 1,
                "supported_roles": model.supported_roles,
                "context_window_tokens": model.context_window_tokens,
                "supports_tool_calling": model.supports_tool_calling,
                "supports_structured_output": model.supports_structured_output,
                "input_cost_per_million_tokens_usd_micros": (
                    model.input_cost_per_million_tokens_usd_micros
                ),
                "output_cost_per_million_tokens_usd_micros": (
                    model.output_cost_per_million_tokens_usd_micros
                ),
            }
        for role, model_id in (
            ("editor", editor_model_id),
            ("helper", helper_model_id),
        ):
            self.store["llm_assignments"][(project_id, role)] = {
                "project_id": project_id,
                "role": role,
                "provider": provider,
                "model_id": model_id,
                "assignment_version": 1,
                "connection_version": 1,
                "inventory_version": 1,
                "catalog_version": CATALOG_VERSION,
            }

    def _llm_execution_snapshot(
        self,
        project_id: str,
        grant: dict[str, Any] | None,
    ) -> str | None:
        if grant is None:
            return None
        assignments = self.conn._active_llm_assignment_rows(
            project_id,
            include_model_metadata=True,
        )
        if [row["role"] for row in assignments] != ["editor", "helper"]:
            return None
        return json.dumps(
            {
                "schema_version": "codegen_llm_execution_snapshot@2",
                "project_id": project_id,
                "repository_grant_id": grant["grant_id"],
                "repository_id": grant["repository_id"],
                "repository_installation_id": grant["installation_id"],
                "repository_full_name": grant["repository_full_name"],
                "codegen_revision": codegen_revision(),
                "behavior_configuration_sha256": (
                    codegen_tenant_behavior_configuration_sha256()
                ),
                "rollout_stage": codegen_rollout_stage().value,
                "assignments": [
                    {
                        "schema_version": "codegen_llm_assignment_snapshot@1",
                        "role": row["role"],
                        "provider": row["provider"],
                        "model_id": row["model_id"],
                        "assignment_version": row["assignment_version"],
                        "connection_version": row["connection_version"],
                        "inventory_version": row["inventory_version"],
                        "catalog_version": row["catalog_version"],
                        "context_window_tokens": row["context_window_tokens"],
                        "supports_tool_calling": row["supports_tool_calling"],
                        "supports_structured_output": row[
                            "supports_structured_output"
                        ],
                        "input_cost_per_million_tokens_usd_micros": row[
                            "input_cost_per_million_tokens_usd_micros"
                        ],
                        "output_cost_per_million_tokens_usd_micros": row[
                            "output_cost_per_million_tokens_usd_micros"
                        ],
                    }
                    for row in assignments
                ],
            },
            sort_keys=True,
        )

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
            "authorized_by_user_id": None,
            "github_user_id": None,
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
        self.add_llm_connection(project_id)

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
            "llm_execution_snapshot": self._llm_execution_snapshot(
                project_id,
                grant,
            ),
            "error": None,
            "created_at": _T0,
            "updated_at": _T0,
        }
