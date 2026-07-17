from types import SimpleNamespace

from starlette.datastructures import Headers

from app.client_ip import client_ip, parse_trusted_proxy_cidrs


def request(*, peer: str, forwarded: list[tuple[bytes, bytes]] = (), trusted=()):
    return SimpleNamespace(
        client=SimpleNamespace(host=peer),
        headers=Headers(raw=forwarded),
        app=SimpleNamespace(state=SimpleNamespace(trusted_proxy_networks=trusted)),
    )


def test_untrusted_peer_cannot_spoof_forwarded_ip():
    value = client_ip(
        request(
            peer="198.51.100.20",
            forwarded=[(b"x-forwarded-for", b"203.0.113.99")],
        )
    )
    assert value == "198.51.100.20"


def test_trusted_proxy_accepts_one_canonical_forwarded_ip():
    trusted = parse_trusted_proxy_cidrs("172.16.0.0/12")
    value = client_ip(
        request(
            peer="172.20.0.4",
            forwarded=[(b"x-forwarded-for", b"203.0.113.99")],
            trusted=trusted,
        )
    )
    assert value == "203.0.113.99"


def test_trusted_proxy_rejects_forwarded_chains():
    trusted = parse_trusted_proxy_cidrs("172.16.0.0/12")
    value = client_ip(
        request(
            peer="172.20.0.4",
            forwarded=[(b"x-forwarded-for", b"203.0.113.99, 198.51.100.1")],
            trusted=trusted,
        )
    )
    assert value == "172.20.0.4"
