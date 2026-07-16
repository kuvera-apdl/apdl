"""Enricher chain — pluggable, declarative enrichment steps.

A transform declares the enrichers it wants by name::

    enrichers = ("device", "geo")

and the framework resolves and runs them in order, merging each one's output
into a single ``enrichment`` dict that ``build_row`` consumes. Later enrichers
win on key conflicts. Enrichers are pure functions of ``(envelope, ctx)`` so
they are trivially testable and deterministic across repeated prototype runs.

The built-ins here are intentionally dependency-free: ``device`` is a
User-Agent heuristic and ``geo`` normalises whatever location signal already
rode in on the envelope. Swapping in a MaxMind-backed geo enricher or a
``ua-parser`` device enricher is a matter of registering a new enricher under
the same name — no transform changes.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from etl.context import EtlContext
    from etl.envelope import CanonicalEnvelope

logger = logging.getLogger(__name__)

_ENRICHERS: dict[str, "BaseEnricher"] = {}


class BaseEnricher(ABC):
    """A single enrichment step. Stateless; registered once and reused."""

    name: ClassVar[str] = ""

    @abstractmethod
    def enrich(
        self, envelope: "CanonicalEnvelope", ctx: "EtlContext"
    ) -> dict[str, Any]:
        """Return a dict of derived fields (may be empty)."""


def register_enricher(cls: type[BaseEnricher]) -> type[BaseEnricher]:
    """Class decorator that registers an enricher under its ``name``."""
    name = getattr(cls, "name", None)
    if not name:
        raise ValueError(
            f"{cls.__name__} must define a non-empty 'name' to register"
        )
    if name in _ENRICHERS and type(_ENRICHERS[name]) is not cls:
        raise ValueError(f"Duplicate enricher name '{name}' ({cls.__name__})")
    _ENRICHERS[name] = cls()
    return cls


def get_enricher(name: str) -> BaseEnricher:
    if name not in _ENRICHERS:
        raise KeyError(
            f"No enricher registered as '{name}'. Known: {sorted(_ENRICHERS)}"
        )
    return _ENRICHERS[name]


def run_enrichers(
    names: tuple[str, ...],
    envelope: "CanonicalEnvelope",
    ctx: "EtlContext",
) -> dict[str, Any]:
    """Run the named enrichers in order, merging their output (later wins).

    A failing enricher is logged and skipped — enrichment is best-effort and
    must never knock a record into the DLQ on its own.
    """
    out: dict[str, Any] = {}
    for name in names:
        try:
            out.update(get_enricher(name).enrich(envelope, ctx))
        except Exception as exc:  # best-effort; never fail the record here
            logger.warning("enricher '%s' failed: %s", name, exc)
    return out


# ---------------------------------------------------------------------------
# Built-in enrichers
# ---------------------------------------------------------------------------


@register_enricher
class DeviceEnricher(BaseEnricher):
    """Derive device_type / browser / os_name from a User-Agent string.

    Looks for a UA string in ``ctx.extra['user_agent']`` (server-side capture)
    or ``payload.context.userAgent``. If none is present it returns ``{}`` so
    the transform falls back to whatever structured ``context`` the SDK sent.
    """

    name = "device"

    _MOBILE = ("iphone", "android", "mobile", "ipod")
    _TABLET = ("ipad", "tablet")
    _BROWSERS = (
        ("edg", "Edge"),
        ("chrome", "Chrome"),
        ("crios", "Chrome"),
        ("firefox", "Firefox"),
        ("fxios", "Firefox"),
        ("safari", "Safari"),
    )
    # Order matters: iOS/Android UAs also contain "like Mac OS X" / "Linux", so
    # the mobile OSes must be matched before the desktop ones.
    _OSES = (
        ("iphone", "iOS"),
        ("ipad", "iOS"),
        ("android", "Android"),
        ("windows", "Windows"),
        ("mac os", "macOS"),
        ("macintosh", "macOS"),
        ("linux", "Linux"),
    )

    def enrich(
        self, envelope: "CanonicalEnvelope", ctx: "EtlContext"
    ) -> dict[str, Any]:
        ua = ctx.extra.get("user_agent") or envelope.payload.get("context", {}).get(
            "userAgent", ""
        )
        if not ua:
            return {}
        ua_l = ua.lower()

        if any(t in ua_l for t in self._TABLET):
            device_type = "tablet"
        elif any(m in ua_l for m in self._MOBILE):
            device_type = "mobile"
        else:
            device_type = "desktop"

        browser = next((label for key, label in self._BROWSERS if key in ua_l), "")
        os_name = next((label for key, label in self._OSES if key in ua_l), "")
        return {"device_type": device_type, "browser": browser, "os_name": os_name}


@register_enricher
class GeoEnricher(BaseEnricher):
    """Normalise the location signal already attached to the envelope.

    Production deployments swap this for an IP -> geo lookup (MaxMind) keyed on
    ``ctx.ip``; the dependency-free default just uppercases the ISO country code
    the SDK supplied and passes region through, demonstrating the chain without
    pulling in a binary database.
    """

    name = "geo"

    def enrich(
        self, envelope: "CanonicalEnvelope", ctx: "EtlContext"
    ) -> dict[str, Any]:
        geo = envelope.payload.get("context", {}).get("geo", {}) or {}
        out: dict[str, Any] = {}
        if geo.get("country"):
            out["country"] = str(geo["country"]).upper()[:2]
        if geo.get("region"):
            out["region"] = geo["region"]
        return out
