"""Transport retry/backoff behavior, exercised against a mocked httpx layer."""

from __future__ import annotations

import httpx
import pytest
import threading

from apdl.transport import Transport, TransportOutcome

URL = "https://ingest.example/v1/events"
FLAGS_URL = "https://config.example/v1/flags"


def make_transport(**kwargs) -> Transport:
    kwargs.setdefault("backoff", ())  # no retries unless a test asks for them
    kwargs.setdefault("sleep", lambda _seconds: None)  # never actually sleep
    return Transport("proj_test_secret", **kwargs)


def test_post_success_sends_auth_headers(httpx_mock):
    httpx_mock.add_response(url=URL, status_code=200)
    transport = make_transport()

    assert transport.post_json(URL, {"events": []}) is TransportOutcome.ACCEPTED

    request = httpx_mock.get_requests()[0]
    assert request.headers["X-API-Key"] == "proj_test_secret"
    assert request.headers["X-APDL-SDK"].startswith("python/")
    transport.close()


@pytest.mark.parametrize("status", [400, 413, 422])
def test_post_payload_status_is_distinct_rejection(httpx_mock, status):
    httpx_mock.add_response(url=URL, status_code=status)
    transport = make_transport()

    assert transport.post_json(URL, {}) is TransportOutcome.PAYLOAD_REJECTED
    assert len(httpx_mock.get_requests()) == 1  # not retried
    transport.close()


@pytest.mark.parametrize("status", [302, 401, 403, 404])
def test_post_non_payload_status_is_permanent_rejection(httpx_mock, status):
    httpx_mock.add_response(url=URL, status_code=status)
    transport = make_transport()

    assert transport.post_json(URL, {}) is TransportOutcome.PERMANENT_REJECTION
    assert len(httpx_mock.get_requests()) == 1  # not retried
    transport.close()


def test_post_redirect_is_permanent_rejection(httpx_mock):
    httpx_mock.add_response(url=URL, status_code=307, headers={"Location": "/moved"})
    transport = make_transport()

    assert transport.post_json(URL, {}) is TransportOutcome.PERMANENT_REJECTION
    assert len(httpx_mock.get_requests()) == 1
    transport.close()


def test_post_non_serializable_json_is_permanent_rejection(httpx_mock):
    transport = make_transport()
    cycle: dict = {}
    cycle["self"] = cycle

    assert transport.post_json(URL, cycle) is TransportOutcome.PERMANENT_REJECTION
    assert httpx_mock.get_requests() == []
    transport.close()


def test_post_retries_on_5xx_then_succeeds(httpx_mock):
    httpx_mock.add_response(url=URL, status_code=503)
    httpx_mock.add_response(url=URL, status_code=200)
    transport = make_transport(backoff=(0.0,))

    assert transport.post_json(URL, {}) is TransportOutcome.ACCEPTED
    assert len(httpx_mock.get_requests()) == 2
    transport.close()


def test_post_exhausts_retries_on_5xx(httpx_mock):
    httpx_mock.add_response(url=URL, status_code=500)
    httpx_mock.add_response(url=URL, status_code=500)
    transport = make_transport(backoff=(0.0,))

    assert transport.post_json(URL, {}) is TransportOutcome.RETRYABLE
    assert len(httpx_mock.get_requests()) == 2  # initial + one retry
    transport.close()


@pytest.mark.parametrize("status", [408, 425, 429])
def test_post_exhausts_retries_on_transient_4xx_as_retryable(httpx_mock, status):
    httpx_mock.add_response(url=URL, status_code=status)
    httpx_mock.add_response(url=URL, status_code=status)
    transport = make_transport(backoff=(0.0,))

    assert transport.post_json(URL, {}) is TransportOutcome.RETRYABLE
    assert len(httpx_mock.get_requests()) == 2
    transport.close()


def test_post_honors_retry_after_on_429(httpx_mock):
    httpx_mock.add_response(url=URL, status_code=429, headers={"Retry-After": "1"})
    httpx_mock.add_response(url=URL, status_code=200)
    slept: list[float] = []
    transport = make_transport(backoff=(0.0,), sleep=slept.append)

    assert transport.post_json(URL, {}) is TransportOutcome.ACCEPTED
    assert 1.0 in slept  # waited the Retry-After interval
    transport.close()


def test_post_ignores_non_positive_retry_after_and_uses_backoff(httpx_mock):
    httpx_mock.add_response(url=URL, status_code=429, headers={"Retry-After": "0"})
    httpx_mock.add_response(url=URL, status_code=200)
    slept: list[float] = []
    transport = make_transport(backoff=(0.25,), sleep=slept.append)

    assert transport.post_json(URL, {}) is TransportOutcome.ACCEPTED
    assert slept == [0.25]
    transport.close()


def test_post_retries_on_network_error(httpx_mock):
    httpx_mock.add_exception(httpx.ConnectError("boom"), url=URL)
    httpx_mock.add_response(url=URL, status_code=200)
    transport = make_transport(backoff=(0.0,))

    assert transport.post_json(URL, {}) is TransportOutcome.ACCEPTED
    assert len(httpx_mock.get_requests()) == 2
    transport.close()


def test_cancel_retries_interrupts_active_backoff_without_skipping_first_request():
    requests = 0

    def retryable_response(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(503)

    client = httpx.Client(transport=httpx.MockTransport(retryable_response))
    transport = Transport(
        "proj_test_secret",
        client=client,
        backoff=(60.0,),
    )
    entered_backoff = threading.Event()
    original_wait = transport._wait_for_retry

    def observed_wait(seconds: float) -> bool:
        entered_backoff.set()
        return original_wait(seconds)

    transport._wait_for_retry = observed_wait  # type: ignore[method-assign]
    outcomes: list[TransportOutcome] = []
    worker = threading.Thread(target=lambda: outcomes.append(transport.post_json(URL, {})))
    worker.start()
    assert entered_backoff.wait(timeout=1)

    transport.cancel_retries()
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert outcomes == [TransportOutcome.RETRYABLE]
    assert requests == 1
    client.close()


def test_cancelled_transport_still_allows_one_final_attempt(httpx_mock):
    httpx_mock.add_response(url=URL, status_code=200)
    transport = make_transport(backoff=(60.0,))
    transport.cancel_retries()

    assert transport.post_json(URL, {}) is TransportOutcome.ACCEPTED
    assert len(httpx_mock.get_requests()) == 1
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

    assert transport.post_json(URL, {}) is TransportOutcome.ACCEPTED
    transport.close()
