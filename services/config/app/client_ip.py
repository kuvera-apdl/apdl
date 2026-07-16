"""Canonical Config client-IP extraction behind a trusted proxy boundary."""

from __future__ import annotations

import logging
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network
from ipaddress import ip_address, ip_network

from fastapi import Request

logger = logging.getLogger(__name__)

IPAddress = IPv4Address | IPv6Address
IPNetwork = IPv4Network | IPv6Network


def parse_trusted_proxy_cidrs(value: str) -> tuple[IPNetwork, ...]:
    networks: list[IPNetwork] = []
    for raw_cidr in value.split(","):
        cidr = raw_cidr.strip()
        if cidr:
            networks.append(ip_network(cidr, strict=True))
    return tuple(networks)


def client_ip(request: Request) -> str:
    """Return the socket peer or one trusted single-hop forwarded address."""
    peer = _parse_ip(request.client.host if request.client else "")
    if peer is None:
        return "unknown"
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
        logger.warning("Ignoring duplicate X-Forwarded-For headers")
        return str(peer)
    forwarded = forwarded_values[0].strip()
    if "," in forwarded:
        logger.warning("Ignoring non-canonical X-Forwarded-For chain")
        return str(peer)
    forwarded_ip = _parse_ip(forwarded)
    if forwarded_ip is None:
        logger.warning("Ignoring invalid X-Forwarded-For value")
        return str(peer)
    return str(forwarded_ip)


def _parse_ip(value: str) -> IPAddress | None:
    try:
        return ip_address(value)
    except ValueError:
        return None
