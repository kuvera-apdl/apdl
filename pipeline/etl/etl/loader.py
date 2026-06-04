"""Loaders — the seam between the framework and a warehouse.

The framework produces rows; a :class:`Loader` decides what to do with them.
This is intentionally the *only* place that would know about ClickHouse, and
nothing here imports a driver — a real writer implements the ``Loader``
protocol (or wraps :class:`BatchingLoader`'s sink) and stays entirely outside
this package.

Two implementations ship:

* :class:`CollectingLoader` — accumulates rows in memory, for tests and dry runs.
* :class:`BatchingLoader` — buffers per target table and flushes through a sink
  callable when a batch fills, which is how a production writer plugs in.
"""

from __future__ import annotations

from typing import Callable, Protocol

from etl.context import Row


class Loader(Protocol):
    """Anything that can accept rows for a target table."""

    def load(self, target: str, rows: list[Row]) -> None: ...


class CollectingLoader:
    """In-memory loader that groups rows by target table. For tests / dry runs."""

    def __init__(self) -> None:
        self.tables: dict[str, list[Row]] = {}

    def load(self, target: str, rows: list[Row]) -> None:
        if rows:
            self.tables.setdefault(target, []).extend(rows)

    def count(self, target: str) -> int:
        return len(self.tables.get(target, []))

    def total(self) -> int:
        return sum(len(rows) for rows in self.tables.values())


class BatchingLoader:
    """Buffers rows per target and flushes through ``sink`` when a batch fills.

    ``sink`` is ``Callable[[str, list[Row]], None]`` — the integration point for
    a real ClickHouse writer, which receives ``(target_table, rows)`` and issues
    the INSERT. The framework never touches the driver; it just decides *when*
    to hand rows over.
    """

    def __init__(
        self, sink: Callable[[str, list[Row]], None], batch_size: int = 1000
    ) -> None:
        self._sink = sink
        self.batch_size = batch_size
        self._buffers: dict[str, list[Row]] = {}

    def load(self, target: str, rows: list[Row]) -> None:
        if not rows:
            return
        buf = self._buffers.setdefault(target, [])
        buf.extend(rows)
        if len(buf) >= self.batch_size:
            self.flush(target)

    def flush(self, target: str | None = None) -> None:
        """Flush one target, or all targets when ``target`` is None."""
        targets = [target] if target is not None else list(self._buffers)
        for t in targets:
            buf = self._buffers.get(t)
            if buf:
                self._sink(t, buf)
                self._buffers[t] = []

    def pending(self, target: str) -> int:
        return len(self._buffers.get(target, []))
