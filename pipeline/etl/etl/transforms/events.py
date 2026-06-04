"""Behavioral-event transforms -> ``events_v2``.

The six SDK event types (track/page/screen/identify/group/alias) all land in the
same canonical table; they differ only in how ``event_name`` is derived and
which payload fields get promoted. So they share one base
(:class:`_EventTransform`) that handles the envelope + flattened context, and
each concrete type overrides just :meth:`event_name` and, where needed,
:meth:`extra_properties`.
"""

from __future__ import annotations

from typing import Any

from etl.base import BaseTransform, _json
from etl.context import EtlContext, Row
from etl.envelope import CanonicalEnvelope
from etl.registry import register_transform

EVENTS_V2_COLUMNS = (
    "_id", "_schema", "_project_id", "_idempotency_key", "_correlation_id",
    "_source", "_occurred_at", "_received_at", "_ip",
    "event_name", "user_id", "anonymous_id", "session_id",
    "country", "region", "device_type", "browser", "os_name", "locale",
    "page_url", "referrer", "sdk_version", "properties", "traits",
)


def _to_ipv6(ip: str) -> str:
    """ClickHouse ``IPv6`` column input; '::' is the zero address fallback."""
    return ip or "::"


class _EventTransform(BaseTransform):
    """Shared mapping for every behavioral event going to ``events_v2``."""

    target_table = "events_v2"
    enrichers = ("device", "geo")
    columns = EVENTS_V2_COLUMNS

    # --- per-type hooks -----------------------------------------------------

    def event_name(self, payload: dict[str, Any]) -> str:
        """The value for the ``event_name`` column."""
        raise NotImplementedError

    def extra_properties(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Payload fields to fold into the ``properties`` JSON column."""
        return {}

    # --- shared row builder -------------------------------------------------

    def build_row(
        self, env: CanonicalEnvelope, ctx: EtlContext, enrichment: dict[str, Any]
    ) -> Row:
        p = env.payload
        context = p.get("context", {}) or {}
        geo = context.get("geo", {}) or {}
        browser = context.get("browser", {}) or {}
        os_ctx = context.get("os", {}) or {}
        device = context.get("device", {}) or {}
        page = context.get("page", {}) or {}
        library = context.get("library", {}) or {}

        properties = {**(p.get("properties") or {}), **self.extra_properties(p)}
        traits = p.get("traits") or {}

        row = self.envelope_columns(env, ctx)
        row["_ip"] = _to_ipv6(ctx.ip)
        row.update(
            {
                "event_name": self.event_name(p),
                "user_id": p.get("user_id") or "",
                "anonymous_id": p.get("anonymous_id", ""),
                "session_id": p.get("session_id", ""),
                # Enrichment wins over the raw structured context when present.
                "country": enrichment.get("country") or geo.get("country", ""),
                "region": enrichment.get("region") or geo.get("region", ""),
                "device_type": enrichment.get("device_type") or device.get("type", ""),
                "browser": enrichment.get("browser") or browser.get("name", ""),
                "os_name": enrichment.get("os_name") or os_ctx.get("name", ""),
                "locale": context.get("locale", ""),
                "page_url": page.get("url", ""),
                "referrer": page.get("referrer", ""),
                "sdk_version": library.get("version", ""),
                "properties": _json(properties),
                "traits": _json(traits) if traits else "",
            }
        )
        return row


@register_transform
class TrackTransform(_EventTransform):
    """A custom user action — the workhorse event type."""

    schema = "track@1"
    description = "Custom tracked event (track@1) -> events_v2."

    def validate(self, env: CanonicalEnvelope, ctx: EtlContext) -> None:
        if not env.payload.get("event"):
            raise ValueError("track@1 payload missing required 'event' name")

    def event_name(self, payload: dict[str, Any]) -> str:
        return payload["event"]


@register_transform
class PageTransform(_EventTransform):
    """A web pageview; name/category fold into properties."""

    schema = "page@1"
    description = "Page view (page@1) -> events_v2."

    def event_name(self, payload: dict[str, Any]) -> str:
        return "page"

    def extra_properties(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": payload.get("name", ""),
            "category": payload.get("category", ""),
        }


@register_transform
class ScreenTransform(_EventTransform):
    """A mobile screen view; mirror of page for native apps."""

    schema = "screen@1"
    description = "Screen view (screen@1) -> events_v2."

    def event_name(self, payload: dict[str, Any]) -> str:
        return "screen"

    def extra_properties(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": payload.get("name", ""),
            "category": payload.get("category", ""),
        }


@register_transform
class IdentifyTransform(_EventTransform):
    """User identification; traits go to the dedicated traits column."""

    schema = "identify@1"
    description = "Identify call (identify@1) -> events_v2."

    def event_name(self, payload: dict[str, Any]) -> str:
        return "identify"


@register_transform
class GroupTransform(_EventTransform):
    """Associate a user with a group/account; group_id folds into properties."""

    schema = "group@1"
    description = "Group call (group@1) -> events_v2."

    def event_name(self, payload: dict[str, Any]) -> str:
        return "group"

    def extra_properties(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"group_id": payload.get("group_id", "")}


@register_transform
class AliasTransform(_EventTransform):
    """Merge two identities; previous_id folds into properties."""

    schema = "alias@1"
    description = "Alias call (alias@1) -> events_v2."

    def event_name(self, payload: dict[str, Any]) -> str:
        return "alias"

    def extra_properties(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"previous_id": payload.get("previous_id", "")}
