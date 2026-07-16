"""Async ClickHouse client wrapper with connection pooling."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from asynch.connection import Connection
from asynch.cursors import DictCursor
from asynch.errors import ErrorCode, ServerException

logger = logging.getLogger(__name__)

_PYFORMAT_PARAM_RE = re.compile(r"%\(([A-Za-z_][A-Za-z0-9_]*)\)s")
_BUDGET_ERROR_CODES = frozenset(
    {
        ErrorCode.CANNOT_ALLOCATE_MEMORY,
        ErrorCode.LIMIT_EXCEEDED,
        ErrorCode.MEMORY_LIMIT_EXCEEDED,
        ErrorCode.RECEIVED_ERROR_TOO_MANY_REQUESTS,
        ErrorCode.SET_SIZE_LIMIT_EXCEEDED,
        ErrorCode.TIMEOUT_EXCEEDED,
        ErrorCode.TOO_MANY_BYTES,
        ErrorCode.TOO_MANY_ROWS,
        ErrorCode.TOO_MANY_ROWS_OR_BYTES,
        ErrorCode.TOO_MANY_SIMULTANEOUS_QUERIES,
    }
)


class QueryBudgetExceeded(RuntimeError):
    """A query exceeded the developer-preview execution budget."""


class QueryConcurrencyExceeded(QueryBudgetExceeded):
    """A project already has the maximum number of active queries."""


def _bounded_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def normalize_query_params(query: str) -> str:
    """Convert pyformat placeholders to the format expected by asynch.

    The sync clickhouse-driver supports ``%(name)s`` placeholders, but asynch
    substitutes parameters via ``str.format`` and expects ``{name}``.
    """
    return _PYFORMAT_PARAM_RE.sub(r"{\1}", query)


class ClickHouseClient:
    """Async ClickHouse client that manages a connection pool.

    Configuration is read from environment variables:
        CLICKHOUSE_HOST  — default "localhost"
        CLICKHOUSE_PORT  — default 9000 (native protocol)
        CLICKHOUSE_USER  — default "default"
        CLICKHOUSE_PASSWORD — default ""
        CLICKHOUSE_DB    — default "apdl"
    """

    def __init__(self) -> None:
        self._host = os.getenv("CLICKHOUSE_HOST", "localhost")
        self._port = int(os.getenv("CLICKHOUSE_PORT", "9000"))
        self._user = os.getenv("CLICKHOUSE_USER", "default")
        self._password = os.getenv("CLICKHOUSE_PASSWORD", "")
        self._database = os.getenv("CLICKHOUSE_DB", "apdl")
        self._pool: list[Connection] = []
        self._pool_size = _bounded_int(
            "CLICKHOUSE_POOL_SIZE", 10, minimum=1, maximum=20
        )
        self._timeout_seconds = _bounded_int(
            "QUERY_TIMEOUT_SECONDS", 10, minimum=1, maximum=30
        )
        self._project_limit = _bounded_int(
            "QUERY_MAX_CONCURRENT_PER_PROJECT", 2, minimum=1, maximum=10
        )
        self._settings = {
            "max_execution_time": self._timeout_seconds,
            "max_rows_to_read": _bounded_int(
                "QUERY_MAX_ROWS_TO_READ", 5_000_000,
                minimum=1_000, maximum=20_000_000,
            ),
            "read_overflow_mode": "throw",
            "max_bytes_to_read": _bounded_int(
                "QUERY_MAX_BYTES_TO_READ", 536_870_912,
                minimum=1_048_576, maximum=1_073_741_824,
            ),
            "max_result_rows": _bounded_int(
                "QUERY_MAX_RESULT_ROWS", 10_000,
                minimum=1, maximum=100_000,
            ),
            "max_result_bytes": _bounded_int(
                "QUERY_MAX_RESULT_BYTES", 16_777_216,
                minimum=1_024, maximum=67_108_864,
            ),
            "result_overflow_mode": "throw",
            "max_memory_usage": _bounded_int(
                "QUERY_MAX_MEMORY_BYTES", 536_870_912,
                minimum=16_777_216, maximum=1_073_741_824,
            ),
            "max_threads": _bounded_int(
                "QUERY_MAX_THREADS", 4, minimum=1, maximum=8
            ),
        }
        self._inflight_by_project: dict[str, int] = {}
        self._inflight_total = 0
        self._inflight_lock = asyncio.Lock()

    async def connect(self) -> None:
        """Pre-warm the connection pool."""
        for _ in range(self._pool_size):
            conn = await self._create_connection()
            self._pool.append(conn)
        logger.info(
            "ClickHouse pool created: %d connections to %s:%d/%s",
            self._pool_size, self._host, self._port, self._database,
        )

    async def _create_connection(self) -> Connection:
        conn = Connection(
            host=self._host,
            port=self._port,
            user=self._user,
            password=self._password,
            database=self._database,
        )
        try:
            await conn.connect()
        except BaseException:
            await self._discard(conn)
            raise
        return conn

    async def _acquire(self) -> Connection:
        """Get a connection from the pool, creating one if the pool is empty."""
        if self._pool:
            return self._pool.pop()
        return await self._create_connection()

    async def _release(self, conn: Connection) -> None:
        """Return a connection to the pool."""
        if len(self._pool) < self._pool_size:
            self._pool.append(conn)
        else:
            await conn.close()

    @staticmethod
    async def _discard(conn: Connection) -> None:
        """Close a connection that may still own server-side query state."""
        try:
            await conn.close()
        except BaseException:
            pass

    @asynccontextmanager
    async def _project_slot(self, params: dict[str, Any] | None):
        project_id = str((params or {}).get("project_id") or "__system__")
        async with self._inflight_lock:
            current = self._inflight_by_project.get(project_id, 0)
            if current >= self._project_limit:
                raise QueryConcurrencyExceeded(
                    f"Project '{project_id}' has {current} active queries"
                )
            if self._inflight_total >= self._pool_size:
                raise QueryConcurrencyExceeded(
                    "Query service has reached its global active-query budget"
                )
            self._inflight_by_project[project_id] = current + 1
            self._inflight_total += 1
        try:
            yield
        finally:
            async with self._inflight_lock:
                self._inflight_total -= 1
                remaining = self._inflight_by_project.get(project_id, 1) - 1
                if remaining <= 0:
                    self._inflight_by_project.pop(project_id, None)
                else:
                    self._inflight_by_project[project_id] = remaining

    async def _execute_cursor(
        self,
        cursor,
        query: str,
        params: dict[str, Any],
    ) -> None:
        cursor.set_settings(dict(self._settings))
        try:
            await asyncio.wait_for(
                cursor.execute(normalize_query_params(query), params),
                timeout=self._timeout_seconds,
            )
        except TimeoutError as exc:
            raise QueryBudgetExceeded(
                f"Query exceeded {self._timeout_seconds}s execution budget"
            ) from exc
        except ServerException as exc:
            if exc.code in _BUDGET_ERROR_CODES:
                raise QueryBudgetExceeded(
                    "ClickHouse rejected the query for exceeding its resource budget"
                ) from exc
            raise

    async def execute(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Execute a query and return all rows as a list of dicts.

        Query templates may use clickhouse-driver's ``%(name)s`` placeholder
        style; this wrapper normalizes them for asynch before execution.
        """
        query_params = params or {}
        async with self._project_slot(query_params):
            conn = await self._acquire()
            try:
                async with conn.cursor(cursor=DictCursor) as cursor:
                    await self._execute_cursor(cursor, query, query_params)
                    rows = await cursor.fetchall()
                    result = [dict(row) for row in rows] if rows else []
            except BaseException:
                # A cancelled, timed-out, or rejected query must not leave a
                # possibly-busy native connection in the reusable pool.
                await self._discard(conn)
                raise
            else:
                await self._release(conn)
            return result

    async def execute_iter(
        self, query: str, params: dict[str, Any] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Execute a query and yield rows one-by-one for streaming large result sets."""
        query_params = params or {}
        async with self._project_slot(query_params):
            conn = await self._acquire()
            reusable = False
            try:
                async with conn.cursor(cursor=DictCursor) as cursor:
                    await self._execute_cursor(cursor, query, query_params)
                    while True:
                        row = await cursor.fetchone()
                        if row is None:
                            break
                        yield dict(row)
                reusable = True
            finally:
                if reusable:
                    await self._release(conn)
                else:
                    await self._discard(conn)

    async def close(self) -> None:
        """Close all pooled connections."""
        for conn in self._pool:
            try:
                await conn.close()
            except Exception:
                pass
        self._pool.clear()
        logger.info("ClickHouse pool closed")

    async def __aenter__(self) -> "ClickHouseClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()
