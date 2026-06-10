"""Thread-safe in-memory store of gate configs with change notification.

Server-side analogue of ``sdk/javascript/src/flags/cache.ts`` (no persistence
layer — process memory only). All public methods are safe to call from the
background refresh thread and application threads concurrently.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from .models import GateConfig, GateConfigSource

ChangeListener = Callable[[list[GateConfig]], None]


class FlagCache:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._flags: dict[str, GateConfig] = {}
        self._sources: dict[str, GateConfigSource] = {}
        self._invalid_sources: dict[str, GateConfigSource] = {}
        self._version = 0
        self._listeners: list[ChangeListener] = []

    def set(
        self,
        flags: list[GateConfig],
        source: GateConfigSource = "memory",
        invalid_keys: list[str] | None = None,
    ) -> None:
        """Replaces the entire cache and notifies listeners."""
        with self._lock:
            self._flags = {flag.key: flag for flag in flags}
            self._sources = {flag.key: source for flag in flags}
            self._invalid_sources = {key: source for key in (invalid_keys or [])}
            self._version += 1
            snapshot = list(self._flags.values())
        self._notify(snapshot)

    def mark_invalid(self, keys: list[str], source: GateConfigSource = "memory") -> None:
        """Marks keys malformed while preserving unrelated cached gates."""
        with self._lock:
            for key in keys:
                self._flags.pop(key, None)
                self._sources.pop(key, None)
                self._invalid_sources[key] = source
            self._version += 1
            snapshot = list(self._flags.values())
        self._notify(snapshot)

    def get(self, key: str) -> GateConfig | None:
        with self._lock:
            return self._flags.get(key)

    def get_all(self) -> list[GateConfig]:
        with self._lock:
            return list(self._flags.values())

    def get_source(self, key: str) -> GateConfigSource | None:
        with self._lock:
            return self._sources.get(key)

    def is_invalid(self, key: str) -> bool:
        with self._lock:
            return key in self._invalid_sources

    def get_invalid_source(self, key: str) -> GateConfigSource | None:
        with self._lock:
            return self._invalid_sources.get(key)

    def get_version(self) -> int:
        with self._lock:
            return self._version

    def on_change(self, listener: ChangeListener) -> Callable[[], None]:
        """Registers a listener; returns an unsubscribe callable."""
        with self._lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe

    def _notify(self, flags: list[GateConfig]) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(flags)
            except Exception:  # noqa: BLE001 - listener errors must not propagate
                pass
