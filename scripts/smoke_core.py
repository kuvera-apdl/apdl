#!/usr/bin/env python3
"""Dependency-free transport smoke for the supported APDL OSS core.

The smoke uses the public HTTP boundaries only. It verifies the browser-key
ceiling, creates and evaluates one server flag with the confidential key,
sends one canonical SDK event through the Compose gateway without retrying,
requires Query to observe exactly one row, and archives the flag by version.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


CONFIDENTIAL_KEY = re.compile(
    r"^proj_(?P<project_id>[A-Za-z0-9]{1,64})_[A-Za-z0-9]{16,128}$"
)
BROWSER_KEY = re.compile(
    r"^client_(?P<project_id>[A-Za-z0-9]{1,64})_[A-Za-z0-9]{16,128}$"
)
FLAG_RESPONSE_FIELDS = {
    "key",
    "project_id",
    "name",
    "state",
    "owners",
    "review_by",
    "description",
    "enabled",
    "default_variant",
    "variants",
    "rules",
    "fallthrough",
    "salt",
    "evaluation_mode",
    "auto_disable",
    "guardrails",
    "disabled_reason",
    "disabled_by",
    "disabled_at",
    "version",
    "created_at",
    "updated_at",
    "archived_at",
}


class SmokeFailure(RuntimeError):
    """A transport, contract, or functional smoke assertion failed."""


@dataclass
class SmokeState:
    run_id: str
    flag_key: str
    event_name: str
    message_id: str
    event_timestamp: str
    event_date: str
    created_version: int | None = None


def _join_url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}{path}"


def _assert_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SmokeFailure(f"{label} must be a JSON object, got {value!r}")
    return value


def _assert_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise SmokeFailure(
            f"{label} fields differ: missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )


def _request_bytes(
    url: str,
    api_key: str | None = None,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    expected_status: set[int] | frozenset[int] = frozenset({200}),
    timeout: float,
) -> tuple[int, bytes]:
    data = None
    headers = {"Accept": "application/json"}
    if api_key is not None:
        headers["X-API-Key"] = api_key
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            status = response.status
            body = response.read()
    except HTTPError as exc:
        status = exc.code
        body = exc.read()
    except (URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise SmokeFailure(f"{method} {url} failed: {reason}") from exc

    if status not in expected_status:
        preview = body.decode("utf-8", errors="replace")[:500]
        raise SmokeFailure(
            f"{method} {url} returned {status}, expected "
            f"{sorted(expected_status)}: {preview!r}"
        )
    return status, body


def _request_json(
    url: str,
    api_key: str | None = None,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    expected_status: set[int] | frozenset[int] = frozenset({200}),
    timeout: float,
) -> tuple[int, Any]:
    status, body = _request_bytes(
        url,
        api_key,
        method=method,
        payload=payload,
        expected_status=expected_status,
        timeout=timeout,
    )
    try:
        decoded = json.loads(body)
    except json.JSONDecodeError as exc:
        preview = body.decode("utf-8", errors="replace")[:500]
        raise SmokeFailure(
            f"{method} {url} returned non-JSON status {status}: {preview!r}"
        ) from exc
    return status, decoded


def _project_id(confidential_key: str | None, browser_key: str | None) -> str:
    confidential = CONFIDENTIAL_KEY.fullmatch(confidential_key or "")
    if confidential is None:
        raise SmokeFailure(
            "--confidential-key/APDL_DEV_API_KEY must match "
            "proj_{project_id}_{16-or-more-character-secret}"
        )
    browser = BROWSER_KEY.fullmatch(browser_key or "")
    if browser is None:
        raise SmokeFailure(
            "--browser-key/APDL_DEV_CLIENT_KEY must match "
            "client_{project_id}_{16-or-more-character-secret}"
        )
    confidential_project = confidential.group("project_id")
    browser_project = browser.group("project_id")
    if confidential_project != browser_project:
        raise SmokeFailure(
            "Confidential and browser credentials must belong to the same project"
        )
    return confidential_project


def _iso_milliseconds(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _flag_payload(flag_key: str) -> dict[str, Any]:
    return {
        "key": flag_key,
        "name": "APDL OSS core transport smoke",
        "state": "active",
        "owners": ["oss-smoke"],
        "enabled": True,
        "description": "Ephemeral server-evaluated release smoke flag",
        "default_variant": "control",
        "variants": [
            {"key": "control", "weight": 1},
            {"key": "treatment", "weight": 0},
        ],
        "rules": [],
        "fallthrough": {
            "rollout": {"percentage": 100.0, "bucket_by": "user_id"}
        },
        "evaluation_mode": "server",
        "auto_disable": False,
        "guardrails": [],
    }


def _evaluation_payload(project_id: str, flag_key: str, run_id: str) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "key": flag_key,
        "context": {
            "user_id": f"smoke-user-{run_id}",
            "attributes": {"source": "oss-core-smoke"},
        },
        "log_exposure": False,
    }


def _count_payload(
    project_id: str,
    event_name: str,
    event_date: str,
) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "start_date": event_date,
        "end_date": event_date,
        "selectors": [{"event_name": event_name, "filters": []}],
    }


def _event_payload(state: SmokeState) -> dict[str, Any]:
    return {
        "events": [
            {
                "event": state.event_name,
                "type": "track",
                "anonymous_id": f"smoke-anonymous-{state.run_id}",
                "timestamp": state.event_timestamp,
                "properties": {
                    "source": "oss-core-smoke",
                    "run_id": state.run_id,
                },
                "context": {
                    "library": {"name": "apdl-oss-smoke", "version": "1"}
                },
                "message_id": state.message_id,
                "session_id": f"smoke-session-{state.run_id}",
            }
        ]
    }


def _check_health(args: argparse.Namespace) -> None:
    checks = (
        ("Ingestion", _join_url(args.ingestion_url, "/health"), "ok"),
        ("Config", _join_url(args.config_url, "/ready"), "ready"),
        ("Query", _join_url(args.query_url, "/ready"), "ready"),
    )
    for name, url, expected in checks:
        _, decoded = _request_json(url, timeout=args.request_timeout)
        body = _assert_object(decoded, f"{name} health response")
        if body.get("status") != expected:
            raise SmokeFailure(
                f"{name} health status must be {expected!r}, got {body!r}"
            )
        print(f"  ok  {name} ready")

    _, gateway_body = _request_bytes(
        _join_url(args.gateway_url, "/"),
        expected_status={200},
        timeout=args.request_timeout,
    )
    if gateway_body != b"apdl gateway ok\n":
        raise SmokeFailure(
            f"Gateway liveness body differs: {gateway_body[:200]!r}"
        )
    print("  ok  SDK gateway ready")

    if args.admin_url:
        _, decoded = _request_json(
            _join_url(args.admin_url, "/api/ready"),
            timeout=args.request_timeout,
        )
        expected = {
            "status": "ready",
            "degraded": True,
            "core": {
                "postgres": "ready",
                "ingestion": "ready",
                "config": "ready",
                "query": "ready",
            },
            "capabilities": {
                "agents": "not_ready",
                "codegen": "not_ready",
            },
        }
        if decoded != expected:
            raise SmokeFailure(f"Admin readiness response differs: {decoded!r}")
        print("  ok  Admin console and backend ready")


def _prove_browser_ceiling(
    args: argparse.Namespace,
    project_id: str,
    state: SmokeState,
) -> None:
    _, identity_value = _request_json(
        _join_url(args.config_url, "/v1/auth/me"),
        args.browser_key,
        timeout=args.request_timeout,
    )
    identity = _assert_object(identity_value, "browser identity")
    _assert_keys(identity, {"credential_id", "project_id", "roles"}, "browser identity")
    if identity["project_id"] != project_id:
        raise SmokeFailure(f"Browser identity project differs: {identity!r}")
    if identity["roles"] != ["config:read", "events:write"]:
        raise SmokeFailure(f"Browser credential has unexpected roles: {identity!r}")

    _, flags_value = _request_json(
        _join_url(args.gateway_url, "/v1/flags"),
        args.browser_key,
        timeout=args.request_timeout,
    )
    flags = _assert_object(flags_value, "browser flag bootstrap")
    _assert_keys(
        flags,
        {"schema_version", "project_id", "flags"},
        "browser flag bootstrap",
    )
    if flags["schema_version"] != 2 or flags["project_id"] != project_id:
        raise SmokeFailure(f"Browser flag bootstrap differs: {flags!r}")
    if not isinstance(flags["flags"], list):
        raise SmokeFailure("browser flag bootstrap flags must be a list")

    _, write_denial = _request_json(
        _join_url(args.config_url, "/v1/admin/flags"),
        args.browser_key,
        method="POST",
        payload=_flag_payload(state.flag_key),
        expected_status={403},
        timeout=args.request_timeout,
    )
    if write_denial != {"detail": "Credential requires role: config:write"}:
        raise SmokeFailure(f"Browser config-write denial differs: {write_denial!r}")

    _, evaluation_denial = _request_json(
        _join_url(args.config_url, "/v1/evaluate"),
        args.browser_key,
        method="POST",
        payload=_evaluation_payload(project_id, state.flag_key, state.run_id),
        expected_status={403},
        timeout=args.request_timeout,
    )
    if evaluation_denial != {
        "detail": "Credential requires role: config:evaluate"
    }:
        raise SmokeFailure(
            f"Browser config-evaluate denial differs: {evaluation_denial!r}"
        )

    _, query_denial = _request_json(
        _join_url(args.query_url, "/v1/query/events/count"),
        args.browser_key,
        method="POST",
        payload=_count_payload(project_id, state.event_name, state.event_date),
        expected_status={401},
        timeout=args.request_timeout,
    )
    if query_denial != {"detail": "Valid API key required"}:
        raise SmokeFailure(f"Browser query denial differs: {query_denial!r}")
    print("  ok  Browser key is limited to event writes and client config reads")


def _create_flag(
    args: argparse.Namespace,
    project_id: str,
    state: SmokeState,
) -> None:
    _, value = _request_json(
        _join_url(args.config_url, "/v1/admin/flags"),
        args.confidential_key,
        method="POST",
        payload=_flag_payload(state.flag_key),
        expected_status={201},
        timeout=args.request_timeout,
    )
    response = _assert_object(value, "flag create response")
    flag = _assert_object(response.get("flag"), "created flag")
    version = flag.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise SmokeFailure(f"Created flag version is invalid: {version!r}")
    # Capture cleanup authority as soon as the successful response exposes it,
    # even if a later strict response assertion detects contract drift.
    state.created_version = version
    _assert_keys(response, {"created", "flag"}, "flag create response")
    if response["created"] is not True:
        raise SmokeFailure(f"Config did not acknowledge flag creation: {response!r}")
    _assert_keys(flag, FLAG_RESPONSE_FIELDS, "created flag")
    expected_values = {
        "key": state.flag_key,
        "project_id": project_id,
        "state": "active",
        "enabled": True,
        "default_variant": "control",
        "evaluation_mode": "server",
        "auto_disable": False,
    }
    for field, expected in expected_values.items():
        if flag.get(field) != expected:
            raise SmokeFailure(
                f"Created flag {field} expected {expected!r}, got {flag.get(field)!r}"
            )
    print(f"  ok  Config created strict server flag at version {version}")


def _evaluate_flag(
    args: argparse.Namespace,
    project_id: str,
    state: SmokeState,
) -> None:
    _, value = _request_json(
        _join_url(args.config_url, "/v1/evaluate"),
        args.confidential_key,
        method="POST",
        payload=_evaluation_payload(project_id, state.flag_key, state.run_id),
        expected_status={200},
        timeout=args.request_timeout,
    )
    response = _assert_object(value, "flag evaluation response")
    _assert_keys(
        response,
        {
            "key",
            "variant",
            "reason",
            "rule_id",
            "rollout_bucket",
            "variant_bucket",
            "rollout_percentage",
            "bucket_by",
            "config_version",
            "source",
        },
        "flag evaluation response",
    )
    expected = {
        "key": state.flag_key,
        "variant": "control",
        "reason": "fallthrough",
        "rule_id": None,
        "rollout_percentage": 100.0,
        "bucket_by": "user_id",
        "config_version": state.created_version,
        "source": "server",
    }
    for field, expected_value in expected.items():
        if response.get(field) != expected_value:
            raise SmokeFailure(
                f"Evaluation {field} expected {expected_value!r}, "
                f"got {response.get(field)!r}"
            )
    if response["rollout_bucket"] is None or response["variant_bucket"] is None:
        raise SmokeFailure(f"Evaluation buckets must be populated: {response!r}")
    print("  ok  Config returned the deterministic server evaluation without exposure logging")


def _send_event_once(args: argparse.Namespace, state: SmokeState) -> None:
    """Make exactly one transport attempt; callers must never retry this write."""
    _, value = _request_json(
        _join_url(args.gateway_url, "/v1/events"),
        args.browser_key,
        method="POST",
        payload=_event_payload(state),
        expected_status={202},
        timeout=args.request_timeout,
    )
    response = _assert_object(value, "event ingestion response")
    if response != {"accepted": 1}:
        raise SmokeFailure(
            f"Gateway must acknowledge exactly one event, got {response!r}"
        )
    print("  ok  SDK gateway accepted one canonical browser event in one attempt")


def _poll_exact_count(
    args: argparse.Namespace,
    project_id: str,
    state: SmokeState,
    *,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    deadline = monotonic() + args.pipeline_timeout
    last_response: Any = None
    while True:
        _, last_response = _request_json(
            _join_url(args.query_url, "/v1/query/events/count"),
            args.confidential_key,
            method="POST",
            payload=_count_payload(project_id, state.event_name, state.event_date),
            expected_status={200},
            timeout=args.request_timeout,
        )
        response = _assert_object(last_response, "event count response")
        _assert_keys(
            response,
            {"results", "total_events", "total_users"},
            "event count response",
        )
        total = response["total_events"]
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise SmokeFailure(f"Query total_events is invalid: {total!r}")
        if total > 1:
            raise SmokeFailure(
                f"Query observed duplicate smoke events: total_events={total}"
            )
        if total == 1:
            if response["total_users"] != 1:
                raise SmokeFailure(f"Query total_users must be 1: {response!r}")
            results = response["results"]
            if not isinstance(results, list) or len(results) != 1:
                raise SmokeFailure(f"Query must return one selector result: {response!r}")
            result = _assert_object(results[0], "event selector result")
            _assert_keys(
                result,
                {"selector", "event_name", "event_count", "unique_users"},
                "event selector result",
            )
            if (
                result["selector"] != state.event_name
                or result["event_name"] != state.event_name
                or result["event_count"] != 1
                or result["unique_users"] != 1
            ):
                raise SmokeFailure(f"Query selector count differs: {result!r}")
            print("  ok  Query observed exactly one smoke event and one actor")
            return
        if monotonic() >= deadline:
            raise SmokeFailure(
                "Query did not observe the smoke event before the pipeline timeout; "
                f"last response={last_response!r}"
            )
        sleep(args.poll_interval)


def _archive_flag(args: argparse.Namespace, state: SmokeState) -> None:
    if state.created_version is None:
        return
    url = _join_url(
        args.config_url,
        f"/v1/admin/flags/{quote(state.flag_key, safe='')}?"
        + urlencode({"version": state.created_version}),
    )
    _, value = _request_json(
        url,
        args.confidential_key,
        method="DELETE",
        expected_status={200},
        timeout=args.request_timeout,
    )
    response = _assert_object(value, "flag archive response")
    _assert_keys(response, {"archived", "flag"}, "flag archive response")
    if response["archived"] is not True:
        raise SmokeFailure(f"Config did not acknowledge flag archive: {response!r}")
    flag = _assert_object(response["flag"], "archived flag")
    _assert_keys(flag, FLAG_RESPONSE_FIELDS, "archived flag")
    if (
        flag.get("key") != state.flag_key
        or flag.get("state") != "archived"
        or flag.get("enabled") is not False
        or not isinstance(flag.get("archived_at"), str)
        or flag.get("version") != state.created_version + 1
    ):
        raise SmokeFailure(f"Archived flag response differs: {flag!r}")
    print("  ok  Config archived the flag with the returned create version")


def _exercise_core(
    args: argparse.Namespace,
    project_id: str,
    state: SmokeState,
) -> None:
    _check_health(args)
    _prove_browser_ceiling(args, project_id, state)
    _create_flag(args, project_id, state)
    _evaluate_flag(args, project_id, state)
    _send_event_once(args, state)
    _poll_exact_count(args, project_id, state)


def _run(args: argparse.Namespace) -> None:
    project_id = _project_id(args.confidential_key, args.browser_key)
    run_id = uuid.uuid4().hex[:16]
    event_time = datetime.now(timezone.utc)
    state = SmokeState(
        run_id=run_id,
        flag_key=f"smoke_core_{run_id}",
        event_name=f"apdl_core_smoke_{run_id}",
        message_id=f"smoke_core_{run_id}",
        event_timestamp=_iso_milliseconds(event_time),
        event_date=event_time.date().isoformat(),
    )
    primary_failure: Exception | None = None
    cleanup_failure: Exception | None = None

    print(f"Core transport smoke {run_id} for project {project_id}")
    try:
        _exercise_core(args, project_id, state)
    except Exception as exc:  # preserve the first failure through cleanup
        primary_failure = exc
    finally:
        if state.created_version is not None:
            try:
                _archive_flag(args, state)
            except Exception as exc:
                cleanup_failure = exc

    if primary_failure is not None:
        if cleanup_failure is not None:
            note = f"Flag cleanup also failed: {cleanup_failure}"
            primary_failure.add_note(note)
            print(note, file=sys.stderr)
        raise primary_failure
    if cleanup_failure is not None:
        raise cleanup_failure
    print("Core transport smoke passed")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confidential-key",
        default=os.environ.get("APDL_DEV_API_KEY"),
        help="Confidential project key (env: APDL_DEV_API_KEY)",
    )
    parser.add_argument(
        "--browser-key",
        default=os.environ.get("APDL_DEV_CLIENT_KEY"),
        help="Browser project key (env: APDL_DEV_CLIENT_KEY)",
    )
    parser.add_argument(
        "--gateway-url",
        default=os.environ.get("APDL_GATEWAY_URL", "http://localhost:8000"),
        help="SDK gateway base URL (env: APDL_GATEWAY_URL)",
    )
    parser.add_argument(
        "--ingestion-url",
        default=os.environ.get("APDL_INGESTION_URL", "http://localhost:8080"),
        help="Ingestion service base URL (env: APDL_INGESTION_URL)",
    )
    parser.add_argument(
        "--config-url",
        default=os.environ.get("APDL_CONFIG_URL", "http://localhost:8081"),
        help="Config service base URL (env: APDL_CONFIG_URL)",
    )
    parser.add_argument(
        "--query-url",
        default=os.environ.get("APDL_QUERY_URL", "http://localhost:8082"),
        help="Query service base URL (env: APDL_QUERY_URL)",
    )
    parser.add_argument(
        "--admin-url",
        default=os.environ.get("APDL_ADMIN_URL"),
        help="Optional Admin console base URL to prove (env: APDL_ADMIN_URL)",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=os.environ.get("APDL_SMOKE_REQUEST_TIMEOUT", "10"),
        help="Per-request timeout seconds (env: APDL_SMOKE_REQUEST_TIMEOUT)",
    )
    parser.add_argument(
        "--pipeline-timeout",
        type=float,
        default=os.environ.get("APDL_SMOKE_PIPELINE_TIMEOUT", "45"),
        help="Event pipeline deadline seconds (env: APDL_SMOKE_PIPELINE_TIMEOUT)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=os.environ.get("APDL_SMOKE_POLL_INTERVAL", "1"),
        help="Query poll interval seconds (env: APDL_SMOKE_POLL_INTERVAL)",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    if (
        args.request_timeout <= 0
        or args.pipeline_timeout <= 0
        or args.poll_interval <= 0
    ):
        print("Smoke failed: timeout values must be positive", file=sys.stderr)
        return 2
    try:
        _run(args)
    except SmokeFailure as exc:
        print(f"Smoke failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Smoke failed unexpectedly: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
