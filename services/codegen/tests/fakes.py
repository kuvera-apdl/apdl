"""In-memory fakes for endpoint tests (no live PostgreSQL).

Mirrors the ASGITransport + fake-pool pattern used by the agents service:
substring-matched query handling over dict-backed tables.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

_T0 = datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc)


class _Txn:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class FakeConn:
    def __init__(self, store: dict[str, dict[str, dict]]) -> None:
        self.store = store

    def transaction(self) -> _Txn:
        return _Txn()

    async def execute(self, query: str, *args: Any) -> None:
        if "SET prompts" in query:
            # set_prompts: (changeset_id, prompts_json)
            row = self.store["changesets"].get(args[0])
            if row is not None:
                row["prompts"] = args[1]
                row["updated_at"] = _T0
        elif "SET contract_bundle" in query:
            row = self.store["changesets"].get(args[0])
            if row is not None:
                row["contract_bundle"] = args[1]
                row["updated_at"] = _T0
        elif "SET requirement_ledger" in query:
            row = self.store["changesets"].get(args[0])
            if row is not None:
                row["requirement_ledger"] = args[1]
                row["updated_at"] = _T0
        elif "SET inspection_snapshot" in query:
            row = self.store["changesets"].get(args[0])
            if row is not None:
                if args[1] is not None:
                    row["inspection_snapshot"] = args[1]
                if args[2] is not None:
                    row["dependency_slice"] = args[2]
                row["updated_at"] = _T0
        elif "SET verification_plan" in query:
            row = self.store["changesets"].get(args[0])
            if row is not None:
                if args[1] is not None:
                    row["verification_plan"] = args[1]
                if args[2] is not None:
                    row["verification_coverage"] = args[2]
                row["updated_at"] = _T0
        elif "SET review_verdict" in query:
            row = self.store["changesets"].get(args[0])
            if row is not None:
                row["review_verdict"] = args[1]
                row["updated_at"] = _T0
        return None

    async def fetchval(self, query: str, *args: Any):
        if "SELECT 1" in query and "FROM" not in query:
            return 1
        if "SELECT status FROM codegen_changesets" in query:
            row = self.store["changesets"].get(args[0])
            return row["status"] if row else None
        raise AssertionError(f"Unexpected fetchval: {query}")

    async def fetchrow(self, query: str, *args: Any):
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
            self.store["connections"][args[0]] = row
            return row
        if "SELECT * FROM codegen_connections" in query:
            return self.store["connections"].get(args[0])
        if "DELETE FROM codegen_connections" in query:
            return self.store["connections"].pop(args[0], None)
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
                "pr_node_id": None,
                "ci_status": None,
                "ci_awaiting_since": None,
                "ci_retry_count": 0,
                "ci_remediation_status": "idle",
                "ci_failure_key": None,
                "ci_failure_summary": None,
                "merge_sha": None,
                "task": args[5],
                "diff_stat": "{}",
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
            self.store["changesets"][args[0]] = row
            return row
        if "JOIN codegen_connections" in query:
            if "cs.pr_number" in query:
                pr_number, repo = args[0], args[1]
                matches = [
                    r
                    for r in self.store["changesets"].values()
                    if r.get("pr_number") == pr_number
                    and (self.store["connections"].get(r["project_id"]) or {}).get("repo") == repo
                ]
                matches.sort(key=lambda r: r["created_at"], reverse=True)
                return matches[0] if matches else None
            # get_changeset_by_branch: route by (branch, repo), repo joined
            # through the project's connection.
            branch, repo = args[0], args[1]
            matches = [
                r
                for r in self.store["changesets"].values()
                if r.get("branch") == branch
                and r["status"] in (
                    "pr_open", "ci_running", "ci_failed", "ci_passed",
                    "unverified_external_ci",
                )
                and (self.store["connections"].get(r["project_id"]) or {}).get("repo") == repo
            ]
            matches.sort(key=lambda r: r["created_at"], reverse=True)
            return matches[0] if matches else None
        if "SELECT * FROM codegen_changesets" in query:
            return self.store["changesets"].get(args[0])
        if "UPDATE codegen_changesets" in query:
            row = self.store["changesets"].get(args[0])
            if row is None:
                return None
            if "ci_retry_count = ci_retry_count + 1" in query:
                row["ci_retry_count"] += 1
                row["ci_remediation_status"] = "repairing"
                row["ci_failure_key"] = args[1]
                row["ci_failure_summary"] = args[2]
            elif "ci_remediation_status = 'exhausted'" in query:
                row["ci_remediation_status"] = "exhausted"
            elif "status = 'ci_running'" in query:
                row["status"] = "ci_running"
                row["ci_status"] = "pending"
                row["ci_remediation_status"] = "awaiting_ci"
                row["error"] = None
            elif "ci_remediation_status = $2" in query:
                row["ci_remediation_status"] = args[1]
                if args[2] is not None:
                    row["error"] = args[2]
            elif "pr_url" in query:
                # mark_pr_open: (id, status, branch, pr_url, pr_number, node_id, diff_stat)
                row["status"] = args[1]
                row["branch"] = args[2]
                row["pr_url"] = args[3]
                row["pr_number"] = args[4]
                row["pr_node_id"] = args[5]
                row["diff_stat"] = args[6]
                # ci_awaiting_since = now() in the real SQL — the CI-wait anchor.
                row["ci_awaiting_since"] = datetime.now(timezone.utc)
            elif "merge_sha" in query:
                # mark_merged: (id, status, merge_sha)
                row["status"] = args[1]
                row["merge_sha"] = args[2]
            elif "ci_status" in query:
                # set_ci_status: (id, status, ci_status)
                row["status"] = args[1]
                row["ci_status"] = args[2]
            else:
                # transition_changeset: (id, status, error)
                row["status"] = args[1]
                if args[2] is not None:
                    row["error"] = args[2]
            row["updated_at"] = _T0
            return row
        raise AssertionError(f"Unexpected fetchrow: {query}")

    async def fetch(self, query: str, *args: Any):
        if "UPDATE codegen_changesets" in query and "status = ANY" in query:
            # fail_stale_changesets sweep. The fake has no clock, so it applies
            # the status filter only (the time deadline is enforced by real SQL),
            # flipping matching transient rows to error and returning their ids.
            transient = set(args[1])
            swept = []
            for row in self.store["changesets"].values():
                if row["status"] in transient:
                    row["status"] = "error"
                    if row.get("error") is None:
                        row["error"] = args[0]
                    row["updated_at"] = _T0
                    swept.append({"changeset_id": row["changeset_id"]})
            return swept
        if "SELECT changeset_id FROM codegen_changesets" in query and "status = ANY" in query:
            # list_syncable_changeset_ids: rows whose status is in the given set.
            wanted = set(args[0])
            rows = [
                {"changeset_id": r["changeset_id"]}
                for r in self.store["changesets"].values()
                if r["status"] in wanted
            ]
            rows.sort(key=lambda r: r["changeset_id"])
            return rows
        if "FROM codegen_changesets" in query and "WHERE project_id" in query:
            rows = [
                r for r in self.store["changesets"].values() if r["project_id"] == args[0]
            ]
            rows.sort(key=lambda r: r["created_at"], reverse=True)
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
    def __init__(self, store: dict[str, dict[str, dict]] | None = None) -> None:
        self.store = store or {"connections": {}, "changesets": {}}
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
        ci_status: str | None = None,
        ci_awaiting_since: datetime | None = None,
        pr_number: int | None = None,
        pr_node_id: str | None = None,
        branch: str | None = None,
        base_branch: str = "main",
        merge_sha: str | None = None,
    ) -> None:
        """Seed a changeset row in an arbitrary lifecycle state (for endpoint tests)."""
        self.store["changesets"][changeset_id] = {
            "changeset_id": changeset_id,
            "project_id": project_id,
            "run_id": None,
            "status": status,
            "base_branch": base_branch,
            "branch": branch,
            "pr_url": f"https://github.com/acme/widgets/pull/{pr_number}" if pr_number else None,
            "pr_number": pr_number,
            "pr_node_id": pr_node_id,
            "ci_status": ci_status,
            "ci_awaiting_since": ci_awaiting_since,
            "ci_retry_count": 0,
            "ci_remediation_status": "idle",
            "ci_failure_key": None,
            "ci_failure_summary": None,
            "merge_sha": merge_sha,
            "task": '{"title": "t", "spec": "spec spec spec"}',
            "diff_stat": "{}",
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
