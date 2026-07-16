"""Bounded SSE admission and project-versioned fan-out."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Literal

from sse_starlette import ServerSentEvent

logger = logging.getLogger(__name__)

QuotaScope = Literal["global", "project", "credential", "ip"]
CloseReason = Literal[
    "client_disconnect",
    "slow_consumer",
    "max_lifetime",
    "server_shutdown",
]


@dataclass(frozen=True)
class SSESettings:
    queue_capacity: int = 256
    max_connections: int = 1000
    max_connections_per_project: int = 100
    max_connections_per_credential: int = 10
    max_connections_per_ip: int = 20
    ping_interval_seconds: float = 15.0
    send_timeout_seconds: float = 10.0
    max_lifetime_seconds: float = 300.0

    def __post_init__(self) -> None:
        integer_values = {
            "queue_capacity": self.queue_capacity,
            "max_connections": self.max_connections,
            "max_connections_per_project": self.max_connections_per_project,
            "max_connections_per_credential": self.max_connections_per_credential,
            "max_connections_per_ip": self.max_connections_per_ip,
        }
        for name, value in integer_values.items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name, value in {
            "ping_interval_seconds": self.ping_interval_seconds,
            "send_timeout_seconds": self.send_timeout_seconds,
            "max_lifetime_seconds": self.max_lifetime_seconds,
        }.items():
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be a positive duration")
        for name in (
            "max_connections_per_project",
            "max_connections_per_credential",
            "max_connections_per_ip",
        ):
            if getattr(self, name) > self.max_connections:
                raise ValueError(f"{name} must not exceed max_connections")


class ConnectionQuotaExceeded(RuntimeError):
    """A bounded SSE admission scope has no remaining capacity."""

    def __init__(self, scope: QuotaScope, limit: int) -> None:
        self.scope = scope
        self.limit = limit
        super().__init__(f"SSE {scope} connection limit reached")


@dataclass(frozen=True)
class ProjectEvent:
    event: ServerSentEvent
    project_version: int


@dataclass
class SSESubscription:
    connection_id: str
    project_id: str
    credential_id: str
    client_ip: str
    queue: asyncio.Queue[ProjectEvent]
    created_at: float
    close_event: asyncio.Event = field(default_factory=asyncio.Event)
    close_reason: CloseReason | None = None


class SSEBroadcaster:
    """Manage one process's bounded SSE connections and update queues."""

    def __init__(
        self,
        settings: SSESettings | None = None,
        *,
        clock=time.monotonic,
    ) -> None:
        self.settings = settings or SSESettings()
        self._clock = clock
        self._connections: dict[str, SSESubscription] = {}
        self._project_counts: Counter[str] = Counter()
        self._credential_counts: Counter[str] = Counter()
        self._ip_counts: Counter[str] = Counter()
        self._rejected_counts: Counter[str] = Counter()
        self._closed_counts: Counter[str] = Counter()
        self._accepted_total = 0
        self._queue_overflow_total = 0
        self._lock = asyncio.Lock()
        self._running = False
        self._maintenance_task: asyncio.Task | None = None
        self._conn_counter = 0

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._maintenance_task = asyncio.create_task(self._maintenance_loop())
        logger.info("SSE broadcaster maintenance task started")

    async def stop(self) -> None:
        if self._running:
            self._running = False
            if self._maintenance_task is not None:
                self._maintenance_task.cancel()
                try:
                    await self._maintenance_task
                except asyncio.CancelledError:
                    pass
                self._maintenance_task = None

        async with self._lock:
            subscriptions = tuple(self._connections.values())
            for subscription in subscriptions:
                self._request_close_locked(subscription, "server_shutdown")
                self._remove_locked(subscription)
        logger.info("SSE broadcaster stopped, all connections closed")

    async def add_connection(
        self,
        project_id: str,
        credential_id: str,
        client_ip: str,
    ) -> SSESubscription:
        """Atomically admit and register one connection within every quota."""
        async with self._lock:
            self._assert_capacity_locked(
                project_id=project_id,
                credential_id=credential_id,
                client_ip=client_ip,
            )
            connection_id = f"sse_{self._conn_counter}"
            self._conn_counter += 1
            subscription = SSESubscription(
                connection_id=connection_id,
                project_id=project_id,
                credential_id=credential_id,
                client_ip=client_ip,
                queue=asyncio.Queue(maxsize=self.settings.queue_capacity),
                created_at=self._clock(),
            )
            self._connections[connection_id] = subscription
            self._project_counts[project_id] += 1
            self._credential_counts[credential_id] += 1
            self._ip_counts[client_ip] += 1
            self._accepted_total += 1
        logger.debug(
            "SSE connection %s admitted for project %s",
            connection_id,
            project_id,
        )
        return subscription

    async def remove_connection(
        self,
        subscription: SSESubscription,
        *,
        reason: CloseReason = "client_disconnect",
    ) -> None:
        async with self._lock:
            current = self._connections.get(subscription.connection_id)
            if current is not subscription:
                return
            if subscription.close_reason is None:
                subscription.close_reason = reason
            self._remove_locked(subscription)

    async def broadcast(
        self,
        project_id: str,
        event_type: str,
        data: str,
        *,
        project_version: int,
    ) -> None:
        """Queue one ordered project event or close a saturated consumer."""
        if (
            isinstance(project_version, bool)
            or not isinstance(project_version, int)
            or project_version < 1
        ):
            raise ValueError("project_version must be a positive integer")
        event = ProjectEvent(
            event=ServerSentEvent(
                data=data,
                event=event_type,
                id=str(project_version),
            ),
            project_version=project_version,
        )
        async with self._lock:
            for subscription in tuple(self._connections.values()):
                if (
                    subscription.project_id != project_id
                    or subscription.close_reason is not None
                ):
                    continue
                try:
                    subscription.queue.put_nowait(event)
                except asyncio.QueueFull:
                    self._queue_overflow_total += 1
                    self._request_close_locked(subscription, "slow_consumer")
                    logger.warning(
                        "SSE slow consumer scheduled for disconnect",
                        extra={
                            "event": "sse_slow_consumer",
                            "project_id": project_id,
                            "connection_id": subscription.connection_id,
                        },
                    )

    async def connection_count(self, project_id: str) -> int:
        async with self._lock:
            return self._project_counts[project_id]

    async def total_connection_count(self) -> int:
        async with self._lock:
            return len(self._connections)

    async def metrics_snapshot(self) -> dict:
        """Return low-cardinality operational metrics without identity labels."""
        async with self._lock:
            return {
                "active_connections": len(self._connections),
                "accepted_total": self._accepted_total,
                "rejected_total": {
                    scope: self._rejected_counts[scope]
                    for scope in ("global", "project", "credential", "ip")
                },
                "closed_total": dict(sorted(self._closed_counts.items())),
                "queue_overflow_total": self._queue_overflow_total,
            }

    async def expire_connections(self) -> None:
        """Signal connections whose hard lifetime has elapsed."""
        now = self._clock()
        async with self._lock:
            for subscription in tuple(self._connections.values()):
                if (
                    subscription.close_reason is None
                    and now - subscription.created_at
                    >= self.settings.max_lifetime_seconds
                ):
                    self._request_close_locked(subscription, "max_lifetime")

    def _assert_capacity_locked(
        self,
        *,
        project_id: str,
        credential_id: str,
        client_ip: str,
    ) -> None:
        checks: tuple[tuple[QuotaScope, int, int], ...] = (
            ("global", len(self._connections), self.settings.max_connections),
            (
                "project",
                self._project_counts[project_id],
                self.settings.max_connections_per_project,
            ),
            (
                "credential",
                self._credential_counts[credential_id],
                self.settings.max_connections_per_credential,
            ),
            ("ip", self._ip_counts[client_ip], self.settings.max_connections_per_ip),
        )
        for scope, current, limit in checks:
            if current >= limit:
                self._rejected_counts[scope] += 1
                raise ConnectionQuotaExceeded(scope, limit)

    def _request_close_locked(
        self,
        subscription: SSESubscription,
        reason: CloseReason,
    ) -> None:
        if subscription.close_reason is not None:
            return
        subscription.close_reason = reason
        subscription.close_event.set()

    def _remove_locked(self, subscription: SSESubscription) -> None:
        self._connections.pop(subscription.connection_id, None)
        self._decrement(self._project_counts, subscription.project_id)
        self._decrement(self._credential_counts, subscription.credential_id)
        self._decrement(self._ip_counts, subscription.client_ip)
        reason = subscription.close_reason or "client_disconnect"
        self._closed_counts[reason] += 1

    @staticmethod
    def _decrement(counts: Counter[str], key: str) -> None:
        counts[key] -= 1
        if counts[key] <= 0:
            del counts[key]

    async def _maintenance_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(1.0)
                await self.expire_connections()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("SSE connection maintenance failed")


def stream_close_event(reason: CloseReason) -> ServerSentEvent:
    """Return the terminal signal clients observe before reconnecting."""
    return ServerSentEvent(
        data=json.dumps(
            {"reason": reason, "snapshot_required": True},
            separators=(",", ":"),
        ),
        event="stream_error",
        retry=1000,
    )
