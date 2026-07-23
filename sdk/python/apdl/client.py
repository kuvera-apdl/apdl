"""The APDL server-side client.

Orchestrates event tracking and local variant feature flag evaluation. Unlike
the browser SDK, identity is explicit per call (a server handles many users),
there is no auto-capture/UI/consent layer, and flag configs are refreshed by
polling the config service rather than over SSE.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from copy import deepcopy
from types import TracebackType
from typing import Any

from .config import APDLConfig
from .flags.cache import FlagCache
from .flags.evaluator import FlagEvaluator
from .flags.models import EvalContext, GateConfig, GateEvaluationResult
from .flags.parse import parse_flag_config_result
from .queue import DeliveryReport, EventQueue
from .transport import Transport
from .types import (
    FEATURE_FLAG_EXPOSURE_EVENT,
    IngestionEvent,
    default_context,
    generate_id,
)

logger = logging.getLogger("apdl")
FEATURE_FLAG_ASSIGNMENT_REASONS = frozenset({"rule_match", "fallthrough"})

# Server-side evaluation is per-request, so a config change can't carry a single
# "new variant" — it signals "configs updated; re-evaluate against your context".
VariantChangeCallback = Callable[[], None]


class APDLClient:
    """Primary entry point. Prefer :meth:`APDL.init` to construct one."""

    def __init__(
        self,
        config: APDLConfig | None = None,
        *,
        api_key: str | None = None,
        endpoint: str | None = None,
        transport: Transport | None = None,
        **kwargs: Any,
    ) -> None:
        if config is None:
            if api_key is None or endpoint is None:
                raise ValueError(
                    "APDL: api_key and endpoint are required when config is omitted"
                )
            config = APDLConfig(api_key=api_key, endpoint=endpoint, **kwargs)
        elif api_key is not None or endpoint is not None or kwargs:
            raise ValueError(
                "APDL: pass either config or explicit api_key/endpoint/options, not both"
            )
        self._config = config

        self._transport = transport or Transport(
            config.api_key, timeout=config.request_timeout, debug=config.debug
        )
        self._queue = EventQueue(config, self._transport)

        self._flag_cache = FlagCache()
        self._evaluator = FlagEvaluator(self._flag_cache)

        self._variant_listeners: dict[str, set[VariantChangeCallback]] = {}
        self._exposure_keys: set[str] = set()
        self._missing_flag_warnings: set[str] = set()
        self._state_lock = threading.Lock()

        # The server SDK has no real user sessions, but the ingestion contract
        # requires a non-empty session_id on reserved exposure events. A stable
        # per-client id satisfies that without inventing a session model.
        self._session_id = generate_id()

        self._poll_stop = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._lifecycle = threading.Condition()
        self._closing = False
        self._closed = False
        self._shutdown_report: DeliveryReport | None = None

        self._flag_cache.on_change(lambda _flags: self._notify_variant_listeners())

        self._queue.start()
        if config.enable_flags:
            self.refresh_flags()
            self._start_flag_poller()

    @property
    def project_id(self) -> str:
        """The project id parsed from the configured ``api_key``."""
        return self._config.project_id

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
        """Associate traits, and optionally assert an anonymous-to-user alias.

        Passing both ``user_id`` and ``anonymous_id`` emits the canonical,
        irreversible alias assertion. Omitting ``anonymous_id`` remains a
        user-trait update and creates no identity link.
        """
        self._enqueue("identify", event="identify", traits=traits,
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
        self._enqueue("group", event="group", group_id=group_id, traits=traits,
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
        self._enqueue("page", event="page", properties=props or None,
                      user_id=user_id, anonymous_id=anonymous_id)

    # ── Feature flags ─────────────────────────────────────────────

    def get_variant(
        self,
        key: str,
        *,
        user_id: str | None = None,
        anonymous_id: str | None = None,
        attributes: dict[str, Any] | None = None,
        page: str = "",
        component: str = "",
        log_exposure: bool | None = None,
    ) -> str | None:
        """Evaluates a variant flag and returns the assigned variant key.

        Returns ``None`` only when the flag is missing or its config is invalid.
        """
        return self.get_variant_details(
            key,
            user_id=user_id,
            anonymous_id=anonymous_id,
            attributes=attributes,
            page=page,
            component=component,
            log_exposure=log_exposure,
        ).variant

    def get_variant_details(
        self,
        key: str,
        *,
        user_id: str | None = None,
        anonymous_id: str | None = None,
        attributes: dict[str, Any] | None = None,
        page: str = "",
        component: str = "",
        log_exposure: bool | None = None,
    ) -> GateEvaluationResult:
        """Evaluates a flag and returns the fully-explained result."""
        context = EvalContext(
            user_id=user_id,
            anonymous_id=anonymous_id,
            attributes=attributes or {},
        )
        result = self._evaluator.evaluate(key, context)
        self._warn_missing_flag(result)
        should_log = self._config.log_exposures if log_exposure is None else log_exposure
        if should_log:
            self._log_exposure(result, context, page, component)
        return result

    def on_variant_change(
        self, key: str, callback: VariantChangeCallback
    ) -> Callable[[], None]:
        """Registers a callback fired when flag ``key``'s config may have changed.

        The callback receives no value (evaluation is per-request); use it to
        bust local caches or re-evaluate flags against your own context. Returns
        an unsubscribe callable.
        """
        with self._state_lock:
            self._variant_listeners.setdefault(key, set()).add(callback)

        def unsubscribe() -> None:
            with self._state_lock:
                listeners = self._variant_listeners.get(key)
                if listeners:
                    listeners.discard(callback)
                    if not listeners:
                        self._variant_listeners.pop(key, None)

        return unsubscribe

    def get_all_variants(
        self,
        *,
        user_id: str | None = None,
        anonymous_id: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> dict[str, str | None]:
        """Evaluates every cached flag for one identity and returns ``{key: variant}``.

        Useful for bootstrapping a downstream client in a single call. Flags that
        are not in the local cache (missing/invalid) are simply absent from the
        result rather than mapped to ``None``. Exposures are **never** logged here:
        returning a bulk snapshot is not the same as exposing a user to each flag.
        """
        return {
            result.key: result.variant
            for result in self.get_all_variant_details(
                user_id=user_id, anonymous_id=anonymous_id, attributes=attributes
            )
        }

    def get_all_variant_details(
        self,
        *,
        user_id: str | None = None,
        anonymous_id: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> list[GateEvaluationResult]:
        """Fully-explained evaluation of every cached flag for one identity.

        Like :meth:`get_all_variants` but preserving each ``GateEvaluationResult``.
        Results are ordered by flag key for stable output. Never logs exposures.
        """
        context = EvalContext(
            user_id=user_id,
            anonymous_id=anonymous_id,
            attributes=attributes or {},
        )
        flags = sorted(self._flag_cache.get_all(), key=lambda flag: flag.key)
        return [self._evaluator.evaluate(flag.key, context) for flag in flags]

    def refresh_flags(self) -> bool:
        """Fetches the latest flag configs from the config service.

        Returns ``True`` if the cache was updated.
        """
        url = f"{self._config.endpoint}/v1/flags"
        data = self._transport.get_json(url)
        if data is None:
            return False
        result = parse_flag_config_result(data)
        if result is None or result.project_id != self._config.project_id:
            return False
        if result.flags or not result.invalid_keys:
            self._flag_cache.set(result.flags, "initial_fetch", result.invalid_keys)
        else:
            self._flag_cache.mark_invalid(result.invalid_keys, "initial_fetch")
        return True

    def set_flags(self, flags: list[GateConfig]) -> None:
        """Overrides cached flag configs directly (useful for testing)."""
        self._flag_cache.set(flags, "memory")

    # ── Lifecycle ─────────────────────────────────────────────────

    def flush(self) -> DeliveryReport:
        """Drain currently queued events and report anything still retryable."""
        return self._queue.flush()

    def shutdown(self) -> DeliveryReport:
        """Fence intake, drain-or-retain events, then close transport.

        Concurrent callers receive the same idempotent report. Retryable events
        remain in memory and are returned in ``undelivered_events``; callers
        that require process-restart durability must persist that snapshot.
        """
        with self._lifecycle:
            while self._closing and not self._closed:
                self._lifecycle.wait()
            if self._closed:
                assert self._shutdown_report is not None
                return _copy_delivery_report(self._shutdown_report)
            self._closing = True

        self._poll_stop.set()
        poll = self._poll_thread
        if poll is not None and poll is not threading.current_thread():
            poll.join()

        try:
            report = self._queue.stop()
        except BaseException:
            with self._lifecycle:
                self._closing = False
                self._lifecycle.notify_all()
            raise

        if report.undelivered:
            logger.warning(
                "APDL: shutdown retained %d retryable events; persist or replay "
                "DeliveryReport.undelivered_events",
                report.undelivered,
            )

        close_error: BaseException | None = None
        try:
            self._transport.close()
        except BaseException as exc:  # preserve the delivery report before surfacing close
            close_error = exc

        with self._lifecycle:
            self._shutdown_report = report
            self._closed = True
            self._closing = False
            self._lifecycle.notify_all()

        if close_error is not None:
            raise close_error
        return _copy_delivery_report(report)

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
        session_id: str | None = None,
    ) -> None:
        with self._lifecycle:
            if self._closing or self._closed:
                raise RuntimeError("APDL: client is shutting down; event rejected")
        if not user_id and not anonymous_id:
            raise ValueError("APDL: track/identify/group/page require user_id or anonymous_id")
        event_data: dict[str, Any] = {
            "event": event,
            "type": type_,
            "context": default_context(),
        }
        optional_fields = {
            "user_id": user_id,
            "anonymous_id": anonymous_id,
            "group_id": group_id,
            "properties": properties,
            "traits": traits,
            "session_id": session_id,
        }
        event_data.update({
            key: value for key, value in optional_fields.items() if value is not None
        })
        model = IngestionEvent.model_validate(event_data, strict=True)
        self._queue.enqueue(model.to_payload())

    def _log_exposure(
        self,
        result: GateEvaluationResult,
        context: EvalContext,
        page: str,
        component: str,
    ) -> None:
        # A fallback variant is still returned for product control flow when a
        # rollout excludes the actor, but exclusion is not an assignment and
        # must never become exposure telemetry.
        if (
            result.variant is None
            or result.reason not in FEATURE_FLAG_ASSIGNMENT_REASONS
        ):
            return
        # An exposure must be attributable to an identity.
        if not context.user_id and not context.anonymous_id:
            return
        identity = (
            f"user:{context.user_id}" if context.user_id
            else f"anon:{context.anonymous_id}"
        )
        dedupe_key = "|".join([
            identity, result.key, str(result.config_version), result.variant
        ])
        with self._state_lock:
            if dedupe_key in self._exposure_keys:
                return
            try:
                self._enqueue(
                    "track",
                    event=FEATURE_FLAG_EXPOSURE_EVENT,
                    properties={
                        "flag_key": result.key,
                        "variant": result.variant,
                        "reason": result.reason,
                        "rule_id": result.rule_id,
                        "rollout_bucket": result.rollout_bucket,
                        "variant_bucket": result.variant_bucket,
                        "rollout_percentage": result.rollout_percentage,
                        "bucket_by": result.bucket_by,
                        "config_version": result.config_version,
                        "source": result.source,
                        "page": page,
                        "component": component,
                    },
                    user_id=context.user_id,
                    anonymous_id=context.anonymous_id,
                    session_id=self._session_id,
                )
            except Exception as exc:
                logger.warning(
                    "APDL: feature flag exposure could not be enqueued: %s", exc
                )
                return
            self._exposure_keys.add(dedupe_key)

    def _warn_missing_flag(self, result: GateEvaluationResult) -> None:
        if result.reason != "not_found":
            return
        with self._state_lock:
            if result.key in self._missing_flag_warnings:
                return
            self._missing_flag_warnings.add(result.key)
        logger.warning(
            "APDL: feature flag '%s' is missing or archived; returning None.", result.key
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

    def _notify_variant_listeners(self) -> None:
        with self._state_lock:
            listeners = [cb for cbs in self._variant_listeners.values() for cb in cbs]
        for listener in listeners:
            try:
                listener()
            except Exception:  # noqa: BLE001
                pass


def _copy_delivery_report(report: DeliveryReport) -> DeliveryReport:
    return DeliveryReport(
        accepted=report.accepted,
        permanently_rejected=report.permanently_rejected,
        undelivered_events=tuple(deepcopy(list(report.undelivered_events))),
    )
