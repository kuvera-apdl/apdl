from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import quote

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import github
from app.config import Settings
from conftest import make_settings

_STATE = "s" * 43
_OAUTH_STATE = "o" * 43
_CODE_CHALLENGE = "c" * 43
_CALLBACK_URL = "http://admin.test/api/github/codegen/callback"
_AUTHORIZATION_ID = "123e4567-e89b-42d3-a456-426614174000"
_FAILURE_LOCATION = "/codegen?github_repository_error=authorization_failed"


def oauth_location(
    *,
    state: str = _OAUTH_STATE,
) -> str:
    return (
        f"{github.GITHUB_WEB_ORIGIN}/login/oauth/authorize"
        f"?client_id=github-client&redirect_uri={quote(_CALLBACK_URL, safe='')}"
        f"&state={state}&code_challenge={_CODE_CHALLENGE}"
        "&code_challenge_method=S256"
    )


def assert_callback_security(response: httpx.Response) -> None:
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["referrer-policy"] == "no-referrer"


def assert_failure_redirect(response: httpx.Response) -> None:
    assert response.status_code == 303
    assert response.headers["location"] == _FAILURE_LOCATION
    assert "Max-Age=0" in correlation_cookie(response)
    assert_callback_security(response)


def correlation_cookie(response: httpx.Response) -> str:
    return next(
        value
        for value in response.headers.get_list("set-cookie")
        if value.startswith(f"{github.CODEGEN_GITHUB_STATE_COOKIE}=")
    )


@asynccontextmanager
async def callback_client(
    transport: httpx.AsyncBaseTransport,
    *,
    settings: Settings | None = None,
    correlation_state: str | None = _STATE,
) -> AsyncIterator[TestClient]:
    app = FastAPI()
    app.state.settings = settings or make_settings()
    app.state.http_client = httpx.AsyncClient(transport=transport)
    app.include_router(github.router)
    try:
        with TestClient(app) as client:
            if correlation_state is not None:
                client.cookies.set(
                    github.CODEGEN_GITHUB_STATE_COOKIE,
                    correlation_state,
                    path=github.CODEGEN_GITHUB_CALLBACK_PATH,
                )
            yield client
    finally:
        await app.state.http_client.aclose()


@pytest.mark.asyncio
async def test_setup_callback_matches_browser_state_and_rotates_cookie() -> None:
    seen: dict[str, object] = {}

    def upstream(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = dict(request.url.params)
        seen["cookie"] = request.headers.get("cookie")
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(
            303,
            headers={
                "Location": oauth_location(),
                "Set-Cookie": "upstream=must-not-escape",
                "Cache-Control": "public, max-age=3600",
                "Referrer-Policy": "unsafe-url",
            },
        )

    async with callback_client(httpx.MockTransport(upstream)) as client:
        client.cookies.set("apdl_admin_session", "not-required", path="/api")
        response = client.get(
            github.CODEGEN_GITHUB_CALLBACK_PATH,
            params={
                "state": _STATE,
                "installation_id": "42",
                "setup_action": "install",
            },
            headers={"Authorization": "Bearer untrusted"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == oauth_location()
    cookie = correlation_cookie(response)
    assert cookie.startswith(
        f"{github.CODEGEN_GITHUB_STATE_COOKIE}={_OAUTH_STATE};"
    )
    assert "HttpOnly" in cookie
    assert "Max-Age=600" in cookie
    assert f"Path={github.CODEGEN_GITHUB_CALLBACK_PATH}" in cookie
    assert "SameSite=lax" in cookie
    assert "Secure" not in cookie
    assert "upstream" not in " ".join(response.headers.get_list("set-cookie"))
    assert_callback_security(response)
    assert seen == {
        "path": "/github/repository-authorization/callback",
        "query": {
            "state": _STATE,
            "installation_id": "42",
            "setup_action": "install",
        },
        "cookie": None,
        "authorization": None,
    }


@pytest.mark.asyncio
async def test_failed_setup_relays_only_generic_admin_error_and_clears_cookie() -> None:
    location = _FAILURE_LOCATION

    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(303, headers={"Location": location})

    async with callback_client(httpx.MockTransport(upstream)) as client:
        response = client.get(
            github.CODEGEN_GITHUB_CALLBACK_PATH,
            params={
                "state": _STATE,
                "installation_id": "42",
                "setup_action": "install",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == location
    assert "Max-Age=0" in correlation_cookie(response)
    assert_callback_security(response)


@pytest.mark.asyncio
async def test_organization_approval_request_relays_project_status_and_clears_cookie() -> None:
    location = (
        "/codegen?github_repository_status=installation_approval_required"
        "&github_repository_project_id=demo"
    )

    def upstream(request: httpx.Request) -> httpx.Response:
        assert dict(request.url.params) == {
            "state": _STATE,
            "setup_action": "request",
        }
        return httpx.Response(303, headers={"Location": location})

    async with callback_client(httpx.MockTransport(upstream)) as client:
        response = client.get(
            github.CODEGEN_GITHUB_CALLBACK_PATH,
            params={"state": _STATE, "setup_action": "request"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == location
    assert "Max-Age=0" in correlation_cookie(response)
    assert_callback_security(response)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location",
    [
        f"/codegen?github_repository_authorization={_AUTHORIZATION_ID}"
        "&github_repository_project_id=demo",
        "http://admin.test/codegen"
        f"?github_repository_authorization={_AUTHORIZATION_ID}"
        "&github_repository_project_id=demo",
        _FAILURE_LOCATION,
    ],
)
async def test_oauth_callback_relays_trusted_admin_destination_and_clears_cookie(
    location: str,
) -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        assert dict(request.url.params) == {"state": _STATE, "code": "oauth_code"}
        return httpx.Response(303, headers={"Location": location})

    async with callback_client(httpx.MockTransport(upstream)) as client:
        response = client.get(
            github.CODEGEN_GITHUB_CALLBACK_PATH,
            params={"state": _STATE, "code": "oauth_code"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == location
    cookie = correlation_cookie(response)
    assert "Max-Age=0" in cookie
    assert f"Path={github.CODEGEN_GITHUB_CALLBACK_PATH}" in cookie
    assert_callback_security(response)


@pytest.mark.asyncio
async def test_oauth_denial_never_reflects_upstream_detail_and_clears_cookie() -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        assert dict(request.url.params) == {
            "state": _STATE,
            "error": "access_denied",
            "error_description": "The user declined authorization",
            "error_uri": "https://docs.github.com/apps/oauth",
        }
        return httpx.Response(
            400,
            json={"detail": "attacker-controlled upstream detail"},
            headers={"Set-Cookie": "upstream=must-not-escape"},
        )

    async with callback_client(httpx.MockTransport(upstream)) as client:
        response = client.get(
            github.CODEGEN_GITHUB_CALLBACK_PATH,
            params={
                "state": _STATE,
                "error": "access_denied",
                "error_description": "The user declined authorization",
                "error_uri": "https://docs.github.com/apps/oauth",
            },
            follow_redirects=False,
        )

    assert_failure_redirect(response)
    assert "upstream" not in " ".join(response.headers.get_list("set-cookie"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        [("state", _STATE), ("unknown", "value")],
        [("state", _STATE), ("state", _STATE)],
        [("state", "too-short")],
        [("state", _STATE)],
        [("state", _STATE), ("installation_id", "42")],
        [("state", _STATE), ("setup_action", "install")],
        [
            ("state", _STATE),
            ("installation_id", "42"),
            ("setup_action", "request"),
        ],
        [("state", _STATE), ("code", "oauth"), ("installation_id", "42")],
        [("state", _STATE), ("error", "denied"), ("setup_action", "install")],
        [("state", _STATE), ("error_description", "missing error")],
        [
            ("state", _STATE),
            ("installation_id", "42"),
            ("setup_action", "delete"),
        ],
        [
            ("state", _STATE),
            ("installation_id", "0"),
            ("setup_action", "install"),
        ],
        [
            ("state", _STATE),
            ("installation_id", "9223372036854775808"),
            ("setup_action", "install"),
        ],
        [("state", _STATE), ("error", "bad error")],
        [("state", _STATE), ("error", "denied"), ("error_uri", "http://evil.test")],
        [("state", _STATE), ("error", "denied"), ("error_uri", "https://[bad")],
    ],
)
async def test_callback_rejects_noncanonical_query_before_upstream(
    query: list[tuple[str, str]],
) -> None:
    called = False

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(
            303,
            headers={
                "Location": "/codegen?github_repository_error=authorization_failed"
            },
        )

    async with callback_client(httpx.MockTransport(upstream)) as client:
        response = client.get(
            github.CODEGEN_GITHUB_CALLBACK_PATH,
            params=query,
            follow_redirects=False,
        )

    assert_failure_redirect(response)
    assert not called


@pytest.mark.asyncio
@pytest.mark.parametrize("browser_state", [None, "t" * 43])
async def test_shared_installation_callback_without_matching_browser_cookie_is_rejected(
    browser_state: str | None,
) -> None:
    called = False

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(303, headers={"Location": oauth_location()})

    async with callback_client(
        httpx.MockTransport(upstream),
        correlation_state=browser_state,
    ) as client:
        response = client.get(
            github.CODEGEN_GITHUB_CALLBACK_PATH,
            params={
                "state": _STATE,
                "installation_id": "42",
                "setup_action": "install",
            },
            follow_redirects=False,
        )

    assert_failure_redirect(response)
    assert not called


@pytest.mark.asyncio
async def test_callback_rejects_ambiguous_duplicate_correlation_cookies() -> None:
    called = False

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(303, headers={"Location": oauth_location()})

    cookie = f"{github.CODEGEN_GITHUB_STATE_COOKIE}={_STATE}"
    async with callback_client(
        httpx.MockTransport(upstream),
        correlation_state=None,
    ) as client:
        response = client.get(
            github.CODEGEN_GITHUB_CALLBACK_PATH,
            params={
                "state": _STATE,
                "installation_id": "42",
                "setup_action": "install",
            },
            headers={"Cookie": f"{cookie}; {cookie}"},
            follow_redirects=False,
        )

    assert_failure_redirect(response)
    assert not called


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location",
    [
        oauth_location().replace("&code_challenge_method=S256", ""),
        oauth_location().replace("S256", "plain"),
        oauth_location().replace(_CODE_CHALLENGE, "short"),
        oauth_location()
        + f"&code_challenge={'d' * 43}",
    ],
)
async def test_setup_callback_rejects_missing_or_invalid_pkce_redirect(
    location: str,
) -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(303, headers={"Location": location})

    async with callback_client(httpx.MockTransport(upstream)) as client:
        response = client.get(
            github.CODEGEN_GITHUB_CALLBACK_PATH,
            params={
                "state": _STATE,
                "installation_id": "42",
                "setup_action": "install",
            },
            follow_redirects=False,
        )

    assert_failure_redirect(response)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location",
    [
        "https://evil.test/codegen"
        f"?github_repository_authorization={_AUTHORIZATION_ID}"
        "&github_repository_project_id=demo",
        "https://[invalid",
        "//evil.test/codegen"
        f"?github_repository_authorization={_AUTHORIZATION_ID}"
        "&github_repository_project_id=demo",
        oauth_location(),
        "https://github.com/login/oauth/authorize?client_id=wrong-origin",
        "http://admin.test/other-page",
        "/other-page",
        "/codegen?unexpected=value",
        "/codegen?github_repository_error=unexpected",
        f"/codegen?github_repository_authorization={_AUTHORIZATION_ID}",
        (
            f"/codegen?github_repository_authorization={_AUTHORIZATION_ID}"
            "&github_repository_project_id=bad/project"
        ),
        (
            "/codegen?github_repository_status=installation_approval_required"
            "&github_repository_project_id=bad/project"
        ),
    ],
)
async def test_callback_rejects_untrusted_or_phase_incorrect_redirect(
    location: str,
) -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(303, headers={"Location": location})

    async with callback_client(httpx.MockTransport(upstream)) as client:
        response = client.get(
            github.CODEGEN_GITHUB_CALLBACK_PATH,
            params={"state": _STATE, "code": "oauth_code"},
            follow_redirects=False,
        )

    assert_failure_redirect(response)


@pytest.mark.asyncio
async def test_callback_replaces_malformed_upstream_response() -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not a callback response")

    async with callback_client(httpx.MockTransport(upstream)) as client:
        response = client.get(
            github.CODEGEN_GITHUB_CALLBACK_PATH,
            params={"state": _STATE, "code": "oauth_code"},
            follow_redirects=False,
        )

    assert_failure_redirect(response)


@pytest.mark.asyncio
async def test_callback_replaces_network_failure() -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sensitive upstream failure", request=request)

    async with callback_client(httpx.MockTransport(upstream)) as client:
        response = client.get(
            github.CODEGEN_GITHUB_CALLBACK_PATH,
            params={"state": _STATE, "code": "oauth_code"},
            follow_redirects=False,
        )

    assert_failure_redirect(response)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location",
    [
        oauth_location().replace("https://github.com", "https://git.enterprise.test"),
        oauth_location().replace("https://github.com", "http://github.com"),
        oauth_location().replace("https://github.com", "https://github.com.evil.test"),
    ],
)
async def test_setup_callback_rejects_noncanonical_github_oauth_origin(
    location: str,
) -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(303, headers={"Location": location})

    async with callback_client(
        httpx.MockTransport(upstream),
    ) as client:
        response = client.get(
            github.CODEGEN_GITHUB_CALLBACK_PATH,
            params={
                "state": _STATE,
                "installation_id": "42",
                "setup_action": "update",
            },
            follow_redirects=False,
        )

    assert_failure_redirect(response)


def test_main_app_includes_public_codegen_github_callback() -> None:
    from app.main import app

    assert any(
        getattr(route, "original_router", None) is github.router
        for route in app.routes
    )
