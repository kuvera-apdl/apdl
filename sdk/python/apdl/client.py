"""The APDL server-side client.

Orchestrates event tracking and local feature gate evaluation. Unlike the
browser SDK, identity is explicit per call (a server handles many users), there
is no auto-capture/UI/consent layer, and gate configs are refreshed by polling
the config service rather than over SSE.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from types import TracebackType
from typing import Any

from .config import APDLConfig
from .flags.cache import FlagCache
from .flags.evaluator import FlagEvaluator
from .flags.models import EvalContext, GateConfig, GateEvaluationResult
from .flags.parse import parse_flag_config_result
from .queue import EventQueue
from .transport import Transport
from .types import (
    FEATURE_FLAG_EXPOSURE_EVENT,
    IngestionEvent,
    default_context,
)

logger = logging.getLogger("apdl")

# Server-side evaluation is per-request, so a config change can't carry a single
# "new value" — it signals "configs updated; re-evaluate against your context".
FlagChangeCallback = Callable[[], None]


class APDLClient:
    """Primary entry point. Prefer :meth:`APDL.init` to construct one."""

    def __init__(
        self,
        config: APDLConfig | None = None,
        *,
        api_key: str | None = None,
        transport: Transport | None = None,
        **kwargs: Any,
    ) -> None:
        if config is None:
            if api_key is None:
                raise ValueError("APDL: api_key (or a config) is required")
            config = APDLConfig(api_key=api_key, **kwargs)
        self._config = config

        self._transport = transport or Transport(
            config.api_key, timeout=config.request_timeout, debug=config.debug
        )
        self._queue = EventQueue(config, self._transport)

        self._flag_cache = FlagCache()
        self._evaluator = FlagEvaluator(self._flag_cache)

        self._flag_listeners: dict[str, set[FlagChangeCallback]] = {}
        self._exposure_keys: set[str] = set()
        self._missing_gate_warnings: set[str] = set()
        self._state_lock = threading.Lock()

        self._poll_stop = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._closed = False

        self._flag_cache.on_change(lambda _flags: self._notify_flag_listeners())

        self._queue.start()
        if config.enable_flags:
            self.refresh_flags()
            self._start_flag_poller()

    # ── Event tracking ────────────────────────────────────────────

    def track(
        self,
        event: str,
        properties: dict[str, Any] | None = None,
        *,
        user_id: str | None = None,
        anonymous_id: str | None = None,
    ) -> None:
        """Records a custom event."""
        self._enqueue("track", event=event, properties=properties,
                      user_id=user_id, anonymous_id=anonymous_id)

    def identify(
        self,
        user_id: str,
        traits: dict[str, Any] | None = None,
        *,
        anonymous_id: str | None = None,
    ) -> None:
        """Associates traits with a user identity."""
        self._enqueue("identify", event="$identify", traits=traits,
                      user_id=user_id, anonymous_id=anonymous_id)

    def group(
        self,
        group_id: str,
        traits: dict[str, Any] | None = None,
        *,
        user_id: str | None = None,
        anonymous_id: str | None = None,
    ) -> None:
        """Associates a user with a group/account."""
        self._enqueue("group", event="$group", group_id=group_id, traits=traits,
                      user_id=user_id, anonymous_id=anonymous_id)

    def page(
        self,
        name: str | None = None,
        properties: dict[str, Any] | None = None,
        *,
        user_id: str | None = None,
        anonymous_id: str | None = None,
    ) -> None:
        """Records a page/screen view."""
        props = dict(properties or {})
        if name is not None:
            props.setdefault("name", name)
        self._enqueue("page", event="$page", properties=props or None,
                      user_id=user_id, anonymous_id=anonymous_id)

    # ── Feature flags ─────────────────────────────────────────────

    def check_gate(
        self,
        key: str,
        *,
        user_id: str | None = None,
        anonymous_id: str | None = None,
        attributes: dict[str, Any] | None = None,
        log_exposure: bool | None = None,
    ) -> bool:
        """Evaluates a boolean feature gate for the given identity."""
        return self.check_gate_details(
            key,
            user_id=user_id,
            anonymous_id=anonymous_id,
            attributes=attributes,
            log_exposure=log_exposure,
        ).value

    def check_gate_details(
        self,
        key: str,
        *,
        user_id: str | None = None,
        anonymous_id: str | None = None,
        attributes: dict[str, Any] | None = None,
        log_exposure: bool | None = None,
    ) -> GateEvaluationResult:
        """Evaluates a gate and returns the fully-explained result."""
        context = EvalContext(
            user_id=user_id,
            anonymous_id=anonymous_id,
            attributes=attributes or {},
        )
        result = self._evaluator.evaluate(key, context)
        self._warn_missing_gate(result)
        should_log = self._config.log_exposures if log_exposure is None else log_exposure
        if should_log:
            self._log_exposure(result, context)
        return result

    def on_flag_change(self, key: str, callback: FlagChangeCallback) -> Callable[[], None]:
        """Registers a callback fired when gate ``key``'s config may have changed.

        The callback receives no value (evaluation is per-request); use it to
        bust local caches or re-evaluate gates against your own context. Returns
        an unsubscribe callable.
        """
        with self._state_lock:
            self._flag_listeners.setdefault(key, set()).add(callback)

        def unsubscribe() -> None:
            with self._state_lock:
                listeners = self._flag_listeners.get(key)
                if listeners:
                    listeners.discard(callback)
                    if not listeners:
                        self._flag_listeners.pop(key, None)

        return unsubscribe

    def refresh_flags(self) -> bool:
        """Fetches the latest gate configs from the config service.

        Returns ``True`` if the cache was updated.
        """
        url = f"{self._config.config_host}/v1/flags"
        data = self._transport.get_json(url)
        if data is None:
            return False
        result = parse_flag_config_result(data)
        if result is None:
            return False
        if result.flags or not result.invalid_keys:
            self._flag_cache.set(result.flags, "initial_fetch", result.invalid_keys)
        else:
            self._flag_cache.mark_invalid(result.invalid_keys, "initial_fetch")
        return True

    def set_flags(self, flags: list[GateConfig]) -> None:
        """Overrides cached gate configs directly (useful for testing)."""
        self._flag_cache.set(flags, "memory")

    # ── Lifecycle ─────────────────────────────────────────────────

    def flush(self) -> None:
        """Blocks until all queued events have been sent (or dropped)."""
        self._queue.flush()

    def shutdown(self) -> None:
        """Stops background threads after a final flush. Idempotent."""
        if self._closed:
            return
        self._closed = True
        self._poll_stop.set()
        poll = self._poll_thread
        if poll is not None:
            poll.join(timeout=self._config.request_timeout)
        self._queue.stop()
        self._transport.close()

    def __enter__(self) -> APDLClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.shutdown()

    @property
    def pending_events(self) -> int:
        return self._queue.pending()

    # ── Internals ─────────────────────────────────────────────────

    def _enqueue(
        self,
        type_: str,
        *,
        event: str,
        user_id: str | None,
        anonymous_id: str | None,
        properties: dict[str, Any] | None = None,
        traits: dict[str, Any] | None = None,
        group_id: str | None = None,
    ) -> None:
        if not user_id and not anonymous_id:
            raise ValueError("APDL: track/identify/group/page require user_id or anonymous_id")
        model = IngestionEvent(
            event=event,
            type=type_,  # type: ignore[arg-type]
            user_id=user_id,
            anonymous_id=anonymous_id,
            group_id=group_id,
            properties=properties,
            traits=traits,
            context=default_context(),
        )
        self._queue.enqueue(model.to_payload())

    def _log_exposure(self, result: GateEvaluationResult, context: EvalContext) -> None:
        if result.reason in ("not_found", "invalid_config"):
            return
        identity = (
            f"user:{context.user_id}" if context.user_id
            else f"anon:{context.anonymous_id}"
        )
        dedupe_key = "|".join([
            identity, result.key, str(result.config_version), str(result.value)
        ])
        with self._state_lock:
            if dedupe_key in self._exposure_keys:
                return
            self._exposure_keys.add(dedupe_key)

        self.track(
            FEATURE_FLAG_EXPOSURE_EVENT,
            {
                "flag_key": result.key,
                "value": result.value,
                "reason": result.reason,
                "rule_id": result.rule_id,
                "bucket": result.bucket,
                "rollout_percentage": result.rollout_percentage,
                "bucket_by": result.bucket_by,
                "config_version": result.config_version,
                "source": result.source,
            },
            user_id=context.user_id,
            anonymous_id=context.anonymous_id,
        )

    def _warn_missing_gate(self, result: GateEvaluationResult) -> None:
        if result.reason != "not_found":
            return
        with self._state_lock:
            if result.key in self._missing_gate_warnings:
                return
            self._missing_gate_warnings.add(result.key)
        logger.warning(
            "APDL: feature gate '%s' is missing or archived; returning false.", result.key
        )

    def _start_flag_poller(self) -> None:
        def _poll() -> None:
            while not self._poll_stop.wait(self._config.flag_poll_interval):
                try:
                    self.refresh_flags()
                except Exception:  # noqa: BLE001 - poller must survive transient errors
                    logger.exception("APDL: flag refresh failed")

        self._poll_thread = threading.Thread(
            target=_poll, name="apdl-flags", daemon=True
        )
        self._poll_thread.start()

    def _notify_flag_listeners(self) -> None:
        with self._state_lock:
            listeners = [cb for cbs in self._flag_listeners.values() for cb in cbs]
        for listener in listeners:
            try:
                listener()
            except Exception:  # noqa: BLE001
                pass
