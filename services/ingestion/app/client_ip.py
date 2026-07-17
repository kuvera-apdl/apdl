"""Canonical client-IP extraction behind an explicit trusted-proxy boundary."""

from __future__ import annotations

import logging
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_address
from ipaddress import ip_network

from fastapi import Request

logger = logging.getLogger(__name__)

IPAddress = IPv4Address | IPv6Address
IPNetwork = IPv4Network | IPv6Network


def parse_trusted_proxy_cidrs(value: str) -> tuple[IPNetwork, ...]:
    """Parse the canonical comma-separated trusted-proxy network setting."""
    networks: list[IPNetwork] = []
    for raw_cidr in value.split(","):
        cidr = raw_cidr.strip()
        if not cidr:
            continue
        networks.append(ip_network(cidr, strict=True))
    return tuple(networks)


def client_ip(request: Request) -> str:
    """Return a canonical client IP without trusting arbitrary forwarding data.

    The public gateway overwrites ``X-Forwarded-For`` with the socket peer. The
    header is considered only when the immediate peer belongs to a configured
    trusted-proxy CIDR, and the internal contract permits exactly one address.
    """
    peer = _parse_ip(request.client.host if request.client else "")
    if peer is None:
        return ""

    trusted_networks: tuple[IPNetwork, ...] = getattr(
        request.app.state,
        "trusted_proxy_networks",
        (),
    )
    if not any(peer in network for network in trusted_networks):
        return str(peer)

    forwarded_values = request.headers.getlist("x-forwarded-for")
    if not forwarded_values:
        return str(peer)
    if len(forwarded_values) != 1:
        logger.warning(
            "Ignoring duplicate X-Forwarded-For headers from trusted proxy"
        )
        return str(peer)

    forwarded = forwarded_values[0].strip()
    if "," in forwarded:
        logger.warning(
            "Ignoring non-canonical X-Forwarded-For chain from trusted proxy"
        )
        return str(peer)

    forwarded_ip = _parse_ip(forwarded)
    if forwarded_ip is None:
        logger.warning("Ignoring invalid X-Forwarded-For value from trusted proxy")
        return str(peer)
    return str(forwarded_ip)


def _parse_ip(value: str) -> IPAddress | None:
    try:
        return ip_address(value)
    except ValueError:
        return None
