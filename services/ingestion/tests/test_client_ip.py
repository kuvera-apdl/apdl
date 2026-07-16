from types import SimpleNamespace

import pytest
from starlette.datastructures import Headers

from app.client_ip import client_ip, parse_trusted_proxy_cidrs


def request_for(
    peer: str,
    *,
    headers: dict[str, str] | Headers | None = None,
    trusted_cidrs: str = "",
):
    return SimpleNamespace(
        client=SimpleNamespace(host=peer),
        headers=headers if isinstance(headers, Headers) else Headers(headers or {}),
        app=SimpleNamespace(
            state=SimpleNamespace(
                trusted_proxy_networks=parse_trusted_proxy_cidrs(trusted_cidrs)
            )
        ),
    )


def test_untrusted_peer_cannot_spoof_forwarded_client_identity():
    request = request_for(
        "198.51.100.20",
        headers={
            "x-forwarded-for": "203.0.113.99",
            "x-real-ip": "203.0.113.98",
        },
    )

    assert client_ip(request) == "198.51.100.20"


def test_trusted_proxy_can_supply_one_canonical_forwarded_address():
    request = request_for(
        "172.20.0.4",
        headers={"x-forwarded-for": "203.0.113.99"},
        trusted_cidrs="172.16.0.0/12",
    )

    assert client_ip(request) == "203.0.113.99"


@pytest.mark.parametrize(
    "forwarded",
    ["203.0.113.99, 198.51.100.4", "not-an-ip"],
)
def test_trusted_proxy_rejects_noncanonical_forwarding_values(forwarded):
    request = request_for(
        "172.20.0.4",
        headers={"x-forwarded-for": forwarded},
        trusted_cidrs="172.16.0.0/12",
    )

    assert client_ip(request) == "172.20.0.4"


def test_trusted_proxy_rejects_duplicate_forwarding_headers():
    request = request_for(
        "172.20.0.4",
        headers=Headers(
            raw=[
                (b"x-forwarded-for", b"203.0.113.99"),
                (b"x-forwarded-for", b"198.51.100.4"),
            ]
        ),
        trusted_cidrs="172.16.0.0/12",
    )

    assert client_ip(request) == "172.20.0.4"


def test_trusted_proxy_configuration_rejects_noncanonical_networks():
    with pytest.raises(ValueError):
        parse_trusted_proxy_cidrs("172.20.0.4/12")
