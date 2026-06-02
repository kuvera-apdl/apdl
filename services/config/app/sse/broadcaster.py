"""SSE broadcaster for pushing real-time config updates to connected clients.

Matches the C++ SSEBroadcaster behavior: per-project connection management,
heartbeat loop, dead-connection cleanup, and SSE message formatting.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SECONDS = 30


class SSEBroadcaster:
    """Manages SSE connections per project and broadcasts events."""

    def __init__(self) -> None:
        # project_id -> list of (connection_id, asyncio.Queue)
        self._connections: dict[str, list[tuple[str, asyncio.Queue]]] = {}
        self._lock = asyncio.Lock()
        self._running = False
        self._heartbeat_task: asyncio.Task | None = None
        self._event_counter = 0
        self._conn_counter = 0

    async def start(self) -> None:
        """Start the heartbeat loop. Idempotent -- a second call is a no-op."""
        if self._running:
            return
        self._running = True
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info("SSE broadcaster heartbeat task started")

    async def stop(self) -> None:
        """Stop the heartbeat loop and clear all connections. Idempotent."""
        if not self._running:
            return
        self._running = False

        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

        async with self._lock:
            self._connections.clear()

        logger.info("SSE broadcaster stopped, all connections cleared")

    def _generate_connection_id(self) -> str:
        conn_id = f"sse_{self._conn_counter}"
        self._conn_counter += 1
        return conn_id

    async def add_connection(
        self, project_id: str, queue: asyncio.Queue
    ) -> str:
        """Register a new SSE connection for a project.

        Returns a unique connection_id.
        """
        conn_id = self._generate_connection_id()
        async with self._lock:
            if project_id not in self._connections:
                self._connections[project_id] = []
            self._connections[project_id].append((conn_id, queue))

        logger.debug(
            "SSE connection %s added for project %s", conn_id, project_id
        )
        return conn_id

    async def remove_connection(
        self, project_id: str, connection_id: str
    ) -> None:
        """Remove a specific connection. No-op if not found."""
        async with self._lock:
            conns = self._connections.get(project_id)
            if conns is None:
                return

            self._connections[project_id] = [
                (cid, q) for cid, q in conns if cid != connection_id
            ]

            if not self._connections[project_id]:
                del self._connections[project_id]

        logger.debug(
            "SSE connection %s removed for project %s",
            connection_id,
            project_id,
        )

    async def broadcast(
        self, project_id: str, event_type: str, data: str
    ) -> None:
        """Send an SSE-formatted message to all connections for a project.

        Dead connections (full queues) are cleaned up automatically.
        """
        event_id = self._event_counter
        self._event_counter += 1

        # Format SSE message matching C++ output:
        #   id: <id>\n
        #   event: <type>\n
        #   data: <line>\n  (for each line in data)
        #   \n
        lines = [f"id: {event_id}\n", f"event: {event_type}\n"]
        for line in data.split("\n"):
            lines.append(f"data: {line}\n")
        lines.append("\n")
        message = "".join(lines)

        async with self._lock:
            conns = self._connections.get(project_id)
            if conns is None:
                logger.debug(
                    "No SSE connections for project %s, broadcast dropped",
                    project_id,
                )
                return

            dead: list[str] = []

            for conn_id, queue in conns:
                try:
                    queue.put_nowait(message)
                except (asyncio.QueueFull, Exception):
                    logger.warning(
                        "SSE write failed for connection %s", conn_id
                    )
                    dead.append(conn_id)

            # Remove dead connections
            if dead:
                dead_set = set(dead)
                self._connections[project_id] = [
                    (cid, q)
                    for cid, q in conns
                    if cid not in dead_set
                ]
                if not self._connections[project_id]:
                    del self._connections[project_id]
                logger.debug(
                    "Removed %d dead SSE connections for project %s",
                    len(dead),
                    project_id,
                )

    async def connection_count(self, project_id: str) -> int:
        """Return the number of active connections for a project."""
        async with self._lock:
            conns = self._connections.get(project_id)
            return len(conns) if conns else 0

    async def total_connection_count(self) -> int:
        """Return the total number of active connections across all projects."""
        async with self._lock:
            return sum(len(c) for c in self._connections.values())

    async def _heartbeat_loop(self) -> None:
        """Send periodic heartbeat comments to all connections.

        Typed heartbeat events keep the connection alive and are observable by
        browser EventSource clients for client-side liveness monitoring.
        """
        heartbeat = "event: heartbeat\ndata: {}\n\n"

        while self._running:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                return

            if not self._running:
                return

            async with self._lock:
                projects_to_remove: list[str] = []

                for project_id, conns in list(self._connections.items()):
                    dead: list[str] = []

                    for conn_id, queue in conns:
                        try:
                            queue.put_nowait(heartbeat)
                        except (asyncio.QueueFull, Exception):
                            logger.debug(
                                "Heartbeat failed for connection %s", conn_id
                            )
                            dead.append(conn_id)

                    if dead:
                        dead_set = set(dead)
                        self._connections[project_id] = [
                            (cid, q)
                            for cid, q in conns
                            if cid not in dead_set
                        ]

                    if not self._connections.get(project_id):
                        projects_to_remove.append(project_id)

                for pid in projects_to_remove:
                    self._connections.pop(pid, None)
