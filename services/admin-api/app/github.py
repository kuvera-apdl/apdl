"""Browser-bound relay for Codegen's GitHub App authorization callback."""

from __future__ import annotations

import json
import re
import secrets
import uuid
from typing import Literal
from urllib.parse import SplitResult, parse_qsl, urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response

from app.config import Settings

router = APIRouter(tags=["GitHub repository authorization"])

CODEGEN_GITHUB_CALLBACK_PATH = "/api/github/codegen/callback"
CODEGEN_GITHUB_STATE_COOKIE = "apdl_codegen_github_state"
GITHUB_WEB_ORIGIN = "https://github.com"

_CODEGEN_CALLBACK_PATH = "/github/repository-authorization/callback"
_CORRELATION_TTL_SECONDS = 600
_CALLBACK_FIELDS = frozenset(
    {
        "state",
        "code",
        "installation_id",
        "setup_action",
        "error",
        "error_description",
        "error_uri",
    }
)
_OPAQUE_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_ERROR_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
_APP_SLUG_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,98}[A-Za-z0-9])?$"
)
_INSTALLATION_ID_PATTERN = re.compile(r"^[1-9][0-9]{0,18}$")
_MAX_GITHUB_ID = 9_223_372_036_854_775_807
_FAILURE_DETAIL = "GitHub repository authorization failed"
_GENERIC_ERROR_REDIRECT = "/codegen?github_repository_error=authorization_failed"
_APPROVAL_REQUIRED_STATUS = "installation_approval_required"
_PROJECT_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{1,64}$")
_NO_STORE_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
}
_START_RESPONSE_FIELDS = {
    "schema_version",
    "authorization_id",
    "installation_url",
    "expires_at",
}


def _single_value_query(request: Request) -> dict[str, str]:
    """Return a duplicate-free allowlisted callback query."""
    values: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        if key not in _CALLBACK_FIELDS:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)
        if key in values:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)
        values[key] = value
    return values


def _bounded_opaque(value: str | None, *, minimum: int, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and minimum <= len(value) <= maximum
        and _OPAQUE_PATTERN.fullmatch(value) is not None
    )


def _bounded_text(
    value: str | None,
    *,
    minimum: int = 1,
    maximum: int,
) -> bool:
    return (
        isinstance(value, str)
        and minimum <= len(value) <= maximum
        and all(0x20 <= ord(character) < 0x7F for character in value)
    )


def _valid_error_uri(value: str | None) -> bool:
    if not _bounded_text(value, maximum=2_048):
        return False
    parsed = _safe_location(value)
    if parsed is None:
        return False
    try:
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
        and port in {None, 443}
    )


def _validated_callback_query(request: Request) -> dict[str, str]:
    values = _single_value_query(request)
    if not _bounded_opaque(values.get("state"), minimum=32, maximum=512):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)

    leg_fields = set(values) - {"state"}
    is_oauth_success = leg_fields == {"code"}
    is_oauth_denial = "error" in leg_fields and leg_fields <= {
        "error",
        "error_description",
        "error_uri",
    }
    is_app_setup = (
        leg_fields == {"installation_id", "setup_action"}
        and values.get("setup_action") in {"install", "update"}
    )
    is_approval_request = (
        leg_fields == {"setup_action"}
        and values.get("setup_action") == "request"
    )
    if not (
        is_oauth_success
        or is_oauth_denial
        or is_app_setup
        or is_approval_request
    ):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)

    code = values.get("code")
    error = values.get("error")
    if code is not None and not _bounded_opaque(code, minimum=1, maximum=512):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)
    if error is not None and (
        not 1 <= len(error) <= 100 or _ERROR_PATTERN.fullmatch(error) is None
    ):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)
    if "error_description" in values and not _bounded_text(
        values["error_description"], minimum=0, maximum=512
    ):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)
    if "error_uri" in values and not _valid_error_uri(values["error_uri"]):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)

    installation_id = values.get("installation_id")
    if installation_id is not None and (
        _INSTALLATION_ID_PATTERN.fullmatch(installation_id) is None
        or int(installation_id) > _MAX_GITHUB_ID
    ):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)
    setup_action = values.get("setup_action")
    if setup_action is not None and setup_action not in {
        "install",
        "update",
        "request",
    }:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)
    return values


def _origin_identity(parsed: SplitResult) -> tuple[str, str, int] | None:
    try:
        if parsed.username is not None or parsed.password is not None:
            return None
        hostname = parsed.hostname.lower() if parsed.hostname is not None else None
        explicit_port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or hostname is None:
        return None
    port = explicit_port or (443 if scheme == "https" else 80)
    return scheme, hostname, port


def _safe_location(location: str) -> SplitResult | None:
    if (
        not location
        or len(location) > 4_096
        or "\\" in location
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in location)
    ):
        return None
    try:
        return urlsplit(location)
    except ValueError:
        return None


def _single_url_query(
    parsed: SplitResult,
    *,
    maximum_fields: int,
) -> dict[str, str] | None:
    try:
        pairs = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=maximum_fields,
        )
    except ValueError:
        return None
    values: dict[str, str] = {}
    for key, value in pairs:
        if key in values:
            return None
        values[key] = value
    return values


def _admin_origins(settings: Settings) -> set[tuple[str, str, int]]:
    return {
        origin
        for allowed in settings.allowed_origins
        if (origin := _origin_identity(urlsplit(allowed))) is not None
    }


def _trusted_admin_redirect_kind(
    location: str,
    settings: Settings,
) -> Literal["success", "approval_required", "error"] | None:
    parsed = _safe_location(location)
    if parsed is None or parsed.path != "/codegen" or parsed.fragment:
        return None
    if parsed.scheme or parsed.netloc:
        identity = _origin_identity(parsed)
        if identity not in _admin_origins(settings):
            return None

    query = _single_url_query(parsed, maximum_fields=2)
    if query is None:
        return None
    if query == {"github_repository_error": "authorization_failed"}:
        return "error"

    project_id = query.get("github_repository_project_id")
    if (
        project_id is None
        or _PROJECT_ID_PATTERN.fullmatch(project_id) is None
    ):
        return None
    authorization_id = query.get("github_repository_authorization")
    if authorization_id is not None and set(query) == {
        "github_repository_authorization",
        "github_repository_project_id",
    }:
        try:
            return (
                "success"
                if str(uuid.UUID(authorization_id)) == authorization_id
                else None
            )
        except ValueError:
            return None
    if query == {
        "github_repository_status": _APPROVAL_REQUIRED_STATUS,
        "github_repository_project_id": project_id,
    }:
        return "approval_required"
    return None


def _trusted_callback_url(value: str, settings: Settings) -> bool:
    parsed = _safe_location(value)
    if (
        parsed is None
        or parsed.path != CODEGEN_GITHUB_CALLBACK_PATH
        or parsed.query
        or parsed.fragment
    ):
        return False
    return _origin_identity(parsed) in _admin_origins(settings)


def _github_oauth_state(location: str, settings: Settings) -> str | None:
    parsed = _safe_location(location)
    if parsed is None or parsed.fragment:
        return None
    github_web = urlsplit(GITHUB_WEB_ORIGIN)
    if _origin_identity(parsed) != _origin_identity(github_web):
        return None
    if parsed.path != "/login/oauth/authorize":
        return None
    query = _single_url_query(parsed, maximum_fields=5)
    if query is None or set(query) != {
        "client_id",
        "redirect_uri",
        "state",
        "code_challenge",
        "code_challenge_method",
    }:
        return None
    state_value = query["state"]
    if not _bounded_opaque(state_value, minimum=32, maximum=512):
        return None
    if not _bounded_text(query["client_id"], maximum=255):
        return None
    if not _bounded_opaque(
        query["code_challenge"], minimum=43, maximum=43
    ) or query["code_challenge_method"] != "S256":
        return None
    if not _trusted_callback_url(query["redirect_uri"], settings):
        return None
    return state_value


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    values: dict[str, object] = {}
    for key, value in pairs:
        if key in values:
            raise ValueError("duplicate JSON field")
        values[key] = value
    return values


def _installation_state(content: bytes) -> str | None:
    try:
        payload = json.loads(content, object_pairs_hook=_unique_json_object)
    except (UnicodeError, ValueError):
        return None
    if (
        not isinstance(payload, dict)
        or set(payload) != _START_RESPONSE_FIELDS
        or payload.get("schema_version")
        != "github_repository_authorization_start@1"
        or not isinstance(payload.get("installation_url"), str)
        or not _bounded_text(payload.get("expires_at"), maximum=64)
    ):
        return None

    authorization_id = payload.get("authorization_id")
    if not isinstance(authorization_id, str):
        return None
    try:
        if str(uuid.UUID(authorization_id)) != authorization_id:
            return None
    except ValueError:
        return None

    parsed = _safe_location(payload["installation_url"])
    github_web = urlsplit(GITHUB_WEB_ORIGIN)
    if (
        parsed is None
        or parsed.fragment
        or _origin_identity(parsed) != _origin_identity(github_web)
    ):
        return None
    installation_path = re.compile(
        r"^/apps/(?P<slug>[^/]+)/installations/new$"
    ).fullmatch(parsed.path)
    if (
        installation_path is None
        or _APP_SLUG_PATTERN.fullmatch(installation_path.group("slug")) is None
    ):
        return None
    query = _single_url_query(parsed, maximum_fields=1)
    if query is None or set(query) != {"state"}:
        return None
    state_value = query["state"]
    if _bounded_opaque(state_value, minimum=32, maximum=512):
        return state_value
    return None


def _set_correlation_cookie(
    response: Response,
    state_value: str,
    settings: Settings,
) -> None:
    response.set_cookie(
        CODEGEN_GITHUB_STATE_COOKIE,
        state_value,
        max_age=_CORRELATION_TTL_SECONDS,
        path=CODEGEN_GITHUB_CALLBACK_PATH,
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )


def _clear_correlation_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        CODEGEN_GITHUB_STATE_COOKIE,
        path=CODEGEN_GITHUB_CALLBACK_PATH,
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )


def _apply_callback_headers(response: Response) -> Response:
    for name, value in _NO_STORE_HEADERS.items():
        response.headers[name] = value
    return response


def _start_failure(settings: Settings, status_code: int) -> JSONResponse:
    response = JSONResponse(
        status_code=status_code,
        content={"detail": _FAILURE_DETAIL},
    )
    _clear_correlation_cookie(response, settings)
    return _apply_callback_headers(response)


def bind_repository_authorization_start_response(
    response: Response,
    content: bytes,
    settings: Settings,
) -> Response:
    """Bind the server-generated setup state to the initiating browser."""
    if response.status_code != status.HTTP_201_CREATED:
        response_status = (
            response.status_code
            if status.HTTP_400_BAD_REQUEST <= response.status_code <= 599
            else status.HTTP_502_BAD_GATEWAY
        )
        return _start_failure(settings, response_status)
    media_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
    state_value = (
        _installation_state(content)
        if media_type == "application/json"
        else None
    )
    if state_value is None:
        return _start_failure(settings, status.HTTP_502_BAD_GATEWAY)
    _set_correlation_cookie(response, state_value, settings)
    return _apply_callback_headers(response)


def _correlation_cookie(request: Request) -> str | None:
    values: list[str] = []
    for header in request.headers.getlist("cookie"):
        for item in header.split(";"):
            name, separator, value = item.strip().partition("=")
            if separator and name == CODEGEN_GITHUB_STATE_COOKIE:
                values.append(value)
    return values[0] if len(values) == 1 else None


def _matching_correlation_state(request: Request, query_state: str) -> str | None:
    cookie_state = _correlation_cookie(request)
    cookie_valid = (
        cookie_state is not None
        and _bounded_opaque(cookie_state, minimum=32, maximum=512)
    )
    comparison_value = cookie_state if cookie_valid else "0" * 43
    matches = secrets.compare_digest(comparison_value, query_state)
    return cookie_state if cookie_valid and matches else None


def _callback_redirect(
    location: str,
    settings: Settings,
    *,
    next_state: str | None = None,
) -> Response:
    response = Response(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Location": location},
    )
    if next_state is None:
        _clear_correlation_cookie(response, settings)
    else:
        _set_correlation_cookie(response, next_state, settings)
    return _apply_callback_headers(response)


def _callback_failure(settings: Settings) -> Response:
    return _callback_redirect(_GENERIC_ERROR_REDIRECT, settings)


@router.get(CODEGEN_GITHUB_CALLBACK_PATH)
async def codegen_github_callback(request: Request) -> Response:
    """Relay a browser-bound GitHub callback without the Strict admin cookie."""
    settings: Settings = request.app.state.settings
    try:
        query = _validated_callback_query(request)
    except HTTPException:
        return _callback_failure(settings)

    correlation_state = _matching_correlation_state(request, query["state"])
    if correlation_state is None:
        return _callback_failure(settings)
    # The query state is only usable after it matches the browser's HttpOnly
    # correlation cookie; forward the cookie value as the authoritative copy.
    query["state"] = correlation_state
    is_setup = set(query) == {"state", "installation_id", "setup_action"}
    is_approval_request = query == {
        "state": correlation_state,
        "setup_action": "request",
    }

    callback_url = (
        f"{settings.service_urls['codegen'].rstrip('/')}{_CODEGEN_CALLBACK_PATH}"
    )
    try:
        upstream = await request.app.state.http_client.get(
            callback_url,
            params=query,
            follow_redirects=False,
        )
    except httpx.HTTPError:
        return _callback_failure(settings)

    if upstream.status_code != status.HTTP_303_SEE_OTHER:
        return _callback_failure(settings)
    location = upstream.headers.get("location", "")
    redirect_kind = _trusted_admin_redirect_kind(location, settings)
    if is_setup:
        oauth_state = _github_oauth_state(location, settings)
        if oauth_state is not None and not secrets.compare_digest(
            oauth_state,
            correlation_state,
        ):
            return _callback_redirect(location, settings, next_state=oauth_state)
        if redirect_kind == "error":
            return _callback_redirect(location, settings)
        return _callback_failure(settings)

    if is_approval_request:
        if redirect_kind in {"approval_required", "error"}:
            return _callback_redirect(location, settings)
        return _callback_failure(settings)

    if redirect_kind not in {"success", "error"}:
        return _callback_failure(settings)
    return _callback_redirect(location, settings)
