"""Async ClickHouse client wrapper with connection pooling."""

from __future__ import annotations

import logging
import os
from typing import Any, AsyncIterator

from asynch.connection import Connection
from asynch.cursors import DictCursor

logger = logging.getLogger(__name__)


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
        self._pool_size = int(os.getenv("CLICKHOUSE_POOL_SIZE", "10"))

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
        await conn.connect()
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

    async def execute(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Execute a query and return all rows as a list of dicts.

        Parameters use ClickHouse's %(name)s format for substitution.
        """
        conn = await self._acquire()
        try:
            async with conn.cursor(cursor=DictCursor) as cursor:
                await cursor.execute(query, params or {})
                rows = await cursor.fetchall()
                result = [dict(row) for row in rows] if rows else []
        except Exception:
            # On error, discard the connection instead of returning it to the pool.
            try:
                await conn.close()
            except Exception:
                pass
            raise
        else:
            await self._release(conn)
        return result

    async def execute_iter(
        self, query: str, params: dict[str, Any] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Execute a query and yield rows one-by-one for streaming large result sets."""
        conn = await self._acquire()
        try:
            async with conn.cursor(cursor=DictCursor) as cursor:
                await cursor.execute(query, params or {})
                while True:
                    row = await cursor.fetchone()
                    if row is None:
                        break
                    yield dict(row)
        finally:
            await self._release(conn)

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
