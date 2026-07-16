"""Strict environment configuration for the admin backend."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlparse

PROJECT_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{1,64}$")
API_KEY_PATTERN = re.compile(
    r"^proj_(?P<project_id>[A-Za-z0-9]{1,64})_[A-Za-z0-9]{16,128}$"
)
SERVICE_NAMES = frozenset({"ingestion", "config", "query", "agents", "codegen"})


def _json_object(name: str, raw: str) -> dict[str, str]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must be a JSON object") from exc
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise ValueError(f"{name} must be a JSON object with string values")
    return value


def _json_origins(raw: str) -> frozenset[str]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("APDL_ADMIN_ALLOWED_ORIGINS must be a JSON array") from exc
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(origin, str) for origin in value)
    ):
        raise ValueError("APDL_ADMIN_ALLOWED_ORIGINS must be a non-empty JSON array")
    origins: set[str] = set()
    for origin in value:
        parsed = urlparse(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.path not in {"", "/"}
        ):
            raise ValueError(f"Invalid admin origin: {origin}")
        origins.add(origin.rstrip("/"))
    return frozenset(origins)


def _bool(name: str, default: str) -> bool:
    value = os.getenv(name, default).lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return value == "true"


def _positive_int(name: str, default: str) -> int:
    try:
        value = int(os.getenv(name, default))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _service_keys() -> dict[str, str]:
    keys = _json_object(
        "APDL_SERVICE_API_KEYS", os.getenv("APDL_SERVICE_API_KEYS", "{}")
    )
    dev_key = os.getenv("APDL_DEV_API_KEY", "")
    if dev_key:
        match = API_KEY_PATTERN.fullmatch(dev_key)
        if match is None:
            raise ValueError(
                "APDL_DEV_API_KEY does not match proj_{project_id}_{secret}"
            )
        project_id = match.group("project_id")
        existing = keys.get(project_id)
        if existing is not None and existing != dev_key:
            raise ValueError(
                f"Conflicting service credentials for project {project_id}"
            )
        keys[project_id] = dev_key

    for project_id, api_key in keys.items():
        if PROJECT_ID_PATTERN.fullmatch(project_id) is None:
            raise ValueError(
                f"Invalid project ID in APDL_SERVICE_API_KEYS: {project_id}"
            )
        match = API_KEY_PATTERN.fullmatch(api_key)
        if match is None or match.group("project_id") != project_id:
            raise ValueError(
                f"Service credential does not belong to project {project_id}"
            )
    return keys


@dataclass(frozen=True)
class Settings:
    postgres_url: str
    service_urls: Mapping[str, str]
    service_api_keys: Mapping[str, str]
    internal_token: str
    allowed_origins: frozenset[str]
    cookie_secure: bool
    session_ttl_seconds: int
    session_idle_seconds: int
    login_failure_limit: int
    login_lock_seconds: int
    max_request_bytes: int

    @classmethod
    def from_env(cls) -> "Settings":
        service_urls = {
            "ingestion": os.getenv("INGESTION_SERVICE_URL", "http://localhost:8080"),
            "config": os.getenv("CONFIG_SERVICE_URL", "http://localhost:8081"),
            "query": os.getenv("QUERY_SERVICE_URL", "http://localhost:8082"),
            "agents": os.getenv("AGENTS_SERVICE_URL", "http://localhost:8083"),
            "codegen": os.getenv("CODEGEN_SERVICE_URL", "http://localhost:8084"),
        }
        for name, url in service_urls.items():
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError(f"Invalid {name} service URL")
        return cls(
            postgres_url=os.getenv(
                "POSTGRES_URL", "postgresql://apdl:apdl_dev@localhost:5432/apdl"
            ),
            service_urls=service_urls,
            service_api_keys=_service_keys(),
            internal_token=os.getenv("APDL_INTERNAL_TOKEN", ""),
            allowed_origins=_json_origins(
                os.getenv("APDL_ADMIN_ALLOWED_ORIGINS", '["http://localhost:5173"]')
            ),
            cookie_secure=_bool("APDL_ADMIN_COOKIE_SECURE", "true"),
            session_ttl_seconds=_positive_int(
                "APDL_ADMIN_SESSION_TTL_SECONDS", "28800"
            ),
            session_idle_seconds=_positive_int(
                "APDL_ADMIN_SESSION_IDLE_SECONDS", "1800"
            ),
            login_failure_limit=_positive_int("APDL_ADMIN_LOGIN_FAILURE_LIMIT", "5"),
            login_lock_seconds=_positive_int("APDL_ADMIN_LOGIN_LOCK_SECONDS", "900"),
            max_request_bytes=_positive_int("APDL_ADMIN_MAX_REQUEST_BYTES", "2097152"),
        )
