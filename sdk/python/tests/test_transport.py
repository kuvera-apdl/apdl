"""Transport retry/backoff behavior, exercised against a mocked httpx layer."""

from __future__ import annotations

import httpx
import pytest

from apdl.transport import Transport

URL = "https://ingest.example/v1/events"
FLAGS_URL = "https://config.example/v1/flags"


def make_transport(**kwargs) -> Transport:
    kwargs.setdefault("backoff", ())  # no retries unless a test asks for them
    kwargs.setdefault("sleep", lambda _seconds: None)  # never actually sleep
    return Transport("proj_test_secret", **kwargs)


def test_post_success_sends_auth_headers(httpx_mock):
    httpx_mock.add_response(url=URL, status_code=200)
    transport = make_transport()

    assert transport.post_json(URL, {"events": []}) is True

    request = httpx_mock.get_requests()[0]
    assert request.headers["X-API-Key"] == "proj_test_secret"
    assert request.headers["X-APDL-SDK"].startswith("python/")
    transport.close()


def test_post_non_retryable_4xx_returns_false(httpx_mock):
    httpx_mock.add_response(url=URL, status_code=400)
    transport = make_transport()

    assert transport.post_json(URL, {}) is False
    assert len(httpx_mock.get_requests()) == 1  # not retried
    transport.close()


def test_post_retries_on_5xx_then_succeeds(httpx_mock):
    httpx_mock.add_response(url=URL, status_code=503)
    httpx_mock.add_response(url=URL, status_code=200)
    transport = make_transport(backoff=(0.0,))

    assert transport.post_json(URL, {}) is True
    assert len(httpx_mock.get_requests()) == 2
    transport.close()


def test_post_exhausts_retries_on_5xx(httpx_mock):
    httpx_mock.add_response(url=URL, status_code=500)
    httpx_mock.add_response(url=URL, status_code=500)
    transport = make_transport(backoff=(0.0,))

    assert transport.post_json(URL, {}) is False
    assert len(httpx_mock.get_requests()) == 2  # initial + one retry
    transport.close()


def test_post_honors_retry_after_on_429(httpx_mock):
    httpx_mock.add_response(url=URL, status_code=429, headers={"Retry-After": "1"})
    httpx_mock.add_response(url=URL, status_code=200)
    slept: list[float] = []
    transport = make_transport(backoff=(0.0,), sleep=slept.append)

    assert transport.post_json(URL, {}) is True
    assert 1.0 in slept  # waited the Retry-After interval
    transport.close()


def test_post_retries_on_network_error(httpx_mock):
    httpx_mock.add_exception(httpx.ConnectError("boom"), url=URL)
    httpx_mock.add_response(url=URL, status_code=200)
    transport = make_transport(backoff=(0.0,))

    assert transport.post_json(URL, {}) is True
    assert len(httpx_mock.get_requests()) == 2
    transport.close()


def test_get_json_returns_parsed_body(httpx_mock):
    httpx_mock.add_response(url=FLAGS_URL, json={"flags": []})
    transport = make_transport()

    assert transport.get_json(FLAGS_URL) == {"flags": []}
    transport.close()


def test_get_json_non_2xx_returns_none(httpx_mock):
    httpx_mock.add_response(url=FLAGS_URL, status_code=404)
    transport = make_transport()

    assert transport.get_json(FLAGS_URL) is None
    transport.close()


def test_get_json_network_error_returns_none(httpx_mock):
    httpx_mock.add_exception(httpx.ConnectError("boom"), url=FLAGS_URL)
    transport = make_transport()

    assert transport.get_json(FLAGS_URL) is None
    transport.close()


@pytest.mark.parametrize("status", [200, 201, 202, 204])
def test_post_treats_all_2xx_as_success(httpx_mock, status):
    httpx_mock.add_response(url=URL, status_code=status)
    transport = make_transport()

    assert transport.post_json(URL, {}) is True
    transport.close()
