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
                "ci_status": None,
                "task": args[5],
                "diff_stat": "{}",
                "error": None,
                "created_at": _T0,
                "updated_at": _T0,
            }
            self.store["changesets"][args[0]] = row
            return row
        if "SELECT * FROM codegen_changesets" in query:
            return self.store["changesets"].get(args[0])
        if "UPDATE codegen_changesets" in query:
            row = self.store["changesets"].get(args[0])
            if row is None:
                return None
            if "pr_url" in query:
                # mark_pr_open: (id, status, branch, pr_url, pr_number, diff_stat)
                row["status"] = args[1]
                row["branch"] = args[2]
                row["pr_url"] = args[3]
                row["pr_number"] = args[4]
                row["diff_stat"] = args[5]
            else:
                # transition_changeset: (id, status, error)
                row["status"] = args[1]
                if args[2] is not None:
                    row["error"] = args[2]
            row["updated_at"] = _T0
            return row
        raise AssertionError(f"Unexpected fetchrow: {query}")

    async def fetch(self, query: str, *args: Any):
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
        self, project_id: str, repo: str = "acme/widgets", installation_id: int = 1
    ) -> None:
        """Seed a repo connection so changeset creation is permitted."""
        self.store["connections"][project_id] = {
            "project_id": project_id,
            "installation_id": installation_id,
            "repo": repo,
            "default_base_branch": "main",
            "policy": "{}",
            "created_at": _T0,
            "updated_at": _T0,
        }
