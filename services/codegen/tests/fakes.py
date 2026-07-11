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
        connected = self._rows("connections").get(row["project_id"])
        return {
            **row,
            "connected_repository": connected.get("repo") if connected else None,
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
            signal.get("signal_id") == scope
            and signal.get("conclusion") == "failed"
            for signal in signals
        )

    async def execute(self, query: str, *args: Any) -> None:
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
        row["updated_at"] = _T0
        return None

    async def fetchval(self, query: str, *args: Any) -> Any:
        if "SELECT 1" in query and "FROM" not in query:
            return 1
        if "SELECT status FROM codegen_changesets" in query:
            row = self._rows("changesets").get(args[0])
            return row["status"] if row else None
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
        if "INSERT INTO codegen_ci_remediation_attempts" in query:
            return self._insert_remediation_attempt(args)
        if "INSERT INTO codegen_ci_remediation_claims" in query:
            if "SELECT $1, $2, $3, $4" in query and not self._failed_observation_is_current(
                args
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
        if "INSERT INTO codegen_connections" in query:
            row = {
                "project_id": args[0],
                "installation_id": args[1],
                "repo": args[2],
                "default_base_branch": args[3],
                "policy": args[4],
                "created_at": _T0,
                "updated_at": _T0,
            }
            self._rows("connections")[args[0]] = row
            return row
        if "SELECT * FROM codegen_connections" in query:
            return self._rows("connections").get(args[0])
        if "DELETE FROM codegen_connections" in query:
            return self._rows("connections").pop(args[0], None)
        if "INSERT INTO codegen_changesets" in query:
            row = {
                "changeset_id": args[0],
                "project_id": args[1],
                "run_id": args[2],
                "status": args[3],
                "base_branch": args[4],
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
                "task": args[5],
                "diff_stat": "{}",
                "prompts": "[]",
                "contract_bundle": None,
                "requirement_ledger": None,
                "inspection_snapshot": None,
                "dependency_slice": None,
                "verification_plan": None,
                "verification_coverage": None,
                "review_verdict": None,
                "error": None,
                "created_at": _T0,
                "updated_at": _T0,
            }
            self._rows("changesets")[args[0]] = row
            return row

        if "SELECT cs.*, conn.repo AS connected_repository" in query:
            return self._connected_changeset(args[0])
        if "JOIN codegen_connections" in query and "cs.head_sha = $1" in query:
            head_sha, repo = args
            values = [
                row
                for row in self._rows("changesets").values()
                if row.get("head_sha") == head_sha
                and row["status"] == "pr_open"
                and (
                    self._rows("connections").get(row["project_id"]) or {}
                ).get("repo")
                == repo
            ]
            values.sort(key=lambda row: row["created_at"], reverse=True)
            return values[0] if values else None
        if "JOIN codegen_connections" in query and "cs.pr_number = $1" in query:
            pr_number, repo = args
            values = [
                row
                for row in self._rows("changesets").values()
                if row.get("pr_number") == pr_number
                and (
                    self._rows("connections").get(row["project_id"]) or {}
                ).get("repo")
                == repo
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

            if "SET status = 'pr_open', branch = $2" in query:
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
            elif "SET external_ci_status = $2, ci_remediation_status = $3" in query:
                if row.get("head_sha") != args[5]:
                    return None
                row["external_ci_status"] = args[1]
                row["ci_remediation_status"] = args[2]
                row["ci_failure_key"] = args[3]
                row["ci_failure_summary"] = args[4]
            elif "SET ci_retry_count = $2, ci_remediation_status = 'diagnosing'" in query:
                row["ci_retry_count"] = args[1]
                row["ci_remediation_status"] = "diagnosing"
                row["ci_failure_key"] = args[2]
                row["ci_failure_summary"] = args[3]
            elif "SET ci_remediation_status = 'exhausted'" in query:
                row["ci_remediation_status"] = "exhausted"
            elif "SET head_sha = $2, external_ci_status = 'pending'" in query:
                row["head_sha"] = args[1]
                row["external_ci_status"] = "pending"
                row["external_ci_awaiting_since"] = datetime.now(timezone.utc)
                row["ci_remediation_status"] = "awaiting_ci"
                row["ci_failure_key"] = None
                row["ci_failure_summary"] = None
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
        if "SELECT changeset_id FROM codegen_changesets" in query and "status = ANY" in query:
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
            "connections",
            "changesets",
            "pull_request_observations",
            "ci_verification_observations",
            "ci_remediation_attempts",
            "ci_remediation_claims",
        ):
            self.store.setdefault(name, {})
        self.conn = FakeConn(self.store)

    def acquire(self) -> _Acquire:
        return _Acquire(self.conn)

    def add_connection(
        self,
        project_id: str,
        repo: str = "acme/widgets",
        installation_id: int = 1,
        policy: str = "{}",
    ) -> None:
        """Seed a repo connection so changeset creation is permitted."""
        self.store["connections"][project_id] = {
            "project_id": project_id,
            "installation_id": installation_id,
            "repo": repo,
            "default_base_branch": "main",
            "policy": policy,
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
    ) -> None:
        """Seed one canonical lifecycle row for endpoint and job tests."""
        self.store["changesets"][changeset_id] = {
            "changeset_id": changeset_id,
            "project_id": project_id,
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
            "error": None,
            "created_at": _T0,
            "updated_at": _T0,
        }
