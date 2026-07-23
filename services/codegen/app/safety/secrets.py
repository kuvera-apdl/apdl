"""Canonical bounded secret detection and redaction for Codegen text boundaries."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final


REDACTION_MARKER: Final = "[REDACTED]"
MAX_SECRET_SCAN_BYTES: Final = 4 * 1024 * 1024
MAX_STRUCTURED_SCAN_NODES: Final = 100_000
MAX_PROTECTED_VALUE_BYTES: Final = 256 * 1024
_SECRET_ENVIRONMENT_MARKERS: Final = (
    "ANTHROPIC",
    "APDL",
    "DATABASE",
    "GEMINI",
    "GITHUB",
    "GOOGLE",
    "OPENAI",
    "PASSWORD",
    "POSTGRES",
    "PRIVATE_KEY",
    "SECRET",
    "TOKEN",
)


class SecretScanLimitError(ValueError):
    """Raised when untrusted input exceeds a secret-scanning work budget."""


def secret_environment_name(name: str) -> bool:
    """Return whether an environment field name can conventionally hold a secret."""
    if not isinstance(name, str):
        raise TypeError("environment field name must be a string")
    upper = name.upper()
    return any(marker in upper for marker in _SECRET_ENVIRONMENT_MARKERS)


@dataclass(frozen=True)
class _SecretPattern:
    kind: str
    expression: re.Pattern[str]
    replacement: str


_PRIVATE_KEY = _SecretPattern(
    "private_key",
    re.compile(
        r"-----BEGIN [^\r\n-]*PRIVATE KEY(?: BLOCK)?-----.*?"
        r"(?:-----END [^\r\n-]*PRIVATE KEY(?: BLOCK)?-----|\Z)",
        re.DOTALL,
    ),
    REDACTION_MARKER,
)
_COMMON_TOKEN_PATTERNS: tuple[_SecretPattern, ...] = (
    _SecretPattern(
        "aws_access_key_id",
        re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
        REDACTION_MARKER,
    ),
    _SecretPattern(
        "github_token",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
        REDACTION_MARKER,
    ),
    _SecretPattern(
        "github_fine_grained_token",
        re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
        REDACTION_MARKER,
    ),
    _SecretPattern(
        "gitlab_token",
        re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),
        REDACTION_MARKER,
    ),
    _SecretPattern(
        "npm_token",
        re.compile(r"\bnpm_[A-Za-z0-9]{20,}\b"),
        REDACTION_MARKER,
    ),
    _SecretPattern(
        "slack_token",
        re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
        REDACTION_MARKER,
    ),
    _SecretPattern(
        "provider_secret_key",
        re.compile(
            r"\b(?:sk-(?:ant-|proj-)?[A-Za-z0-9_-]{16,}|"
            r"(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,})\b"
        ),
        REDACTION_MARKER,
    ),
    _SecretPattern(
        "google_api_key",
        re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
        REDACTION_MARKER,
    ),
    _SecretPattern(
        "json_web_token",
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\."
            r"[A-Za-z0-9_-]{8,}\b"
        ),
        REDACTION_MARKER,
    ),
)
_CREDENTIAL_URL = _SecretPattern(
    "credential_url",
    re.compile(
        r"(?i)(?P<scheme>\b[a-z][a-z0-9+.-]*://)"
        r"(?P<userinfo>[^/\s:@]+:[^@\s/]+)@(?P<target>[^\s]+)"
    ),
    rf"\g<scheme>{REDACTION_MARKER}",
)
_AUTHORIZATION_HEADER = _SecretPattern(
    "authorization_header",
    re.compile(
        r"(?im)\b(?P<label>authorization|proxy-authorization)"
        r"(?P<separator>[ \t]*[:=][ \t]*)(?P<scheme>bearer|basic)[ \t]+"
        r"(?!\[REDACTED\])(?P<value>[A-Za-z0-9._~+/=-]{4,})"
    ),
    rf"\g<label>\g<separator>\g<scheme> {REDACTION_MARKER}",
)
_COOKIE_HEADER = _SecretPattern(
    "cookie_header",
    re.compile(
        r"(?im)\b(?P<label>cookie|set-cookie)(?P<separator>[ \t]*:[ \t]*)"
        r"(?![ \t]*\[REDACTED\])(?P<value>[^\s\r\n{}][^\r\n{}]{3,})"
    ),
    rf"\g<label>\g<separator>{REDACTION_MARKER}",
)
_BARE_BEARER = _SecretPattern(
    "bearer_token",
    re.compile(
        r"(?i)\bbearer\s+(?!\[REDACTED\])"
        r"(?P<value>[A-Za-z0-9._~+/=-]{16,})"
    ),
    f"Bearer {REDACTION_MARKER}",
)
_QUERY_SECRET = _SecretPattern(
    "url_query_secret",
    re.compile(
        r"(?i)(?P<prefix>[?&](?:access_token|api[_-]?key|token|password|"
        r"secret|client_secret|refresh_token)=)"
        r"(?!\[REDACTED\])(?P<value>[^&#\s\"'{}]{4,})"
    ),
    rf"\g<prefix>{REDACTION_MARKER}",
)
_SECRET_LABEL_PATTERN = (
    r"[_-]?(?:[a-z0-9]+[_-])*(?:secret[_-]?access[_-]?key|"
    r"access[_-]?key[_-]?id|"
    r"access[_-]?token|access[_-]?key|api[_-]?key|private[_-]?key|"
    r"client[_-]?secret|refresh[_-]?token|session[_-]?token|auth[_-]?token|"
    r"id[_-]?token|token|password|passphrase|secret|database[_-]?url|"
    r"redis[_-]?url|postgres[_-]?url|connection[_-]?string)"
)
_NAMED_QUOTED_SECRET = _SecretPattern(
    "named_secret",
    re.compile(
        rf"(?i)(?P<label_quote>[\"']?)\b(?P<label>{_SECRET_LABEL_PATTERN})"
        r"(?P=label_quote)(?P<separator>[ \t]*[:=][ \t]*)"
        r"(?P<value_quote>[\"'])(?!\[REDACTED\])"
        r"(?P<value>(?:(?!(?P=value_quote))[^\r\n]){4,}?)"
        r"(?P=value_quote)"
    ),
    rf"\g<label_quote>\g<label>\g<label_quote>\g<separator>"
    rf"\g<value_quote>{REDACTION_MARKER}\g<value_quote>",
)
_NAMED_UNQUOTED_SECRET = _SecretPattern(
    "named_secret",
    re.compile(
        rf"(?i)\b(?P<label>{_SECRET_LABEL_PATTERN})"
        r"(?P<separator>[ \t]*[:=][ \t]*)(?!\[REDACTED\])"
        r"(?P<value>(?=[^\s,;\"'&(){}\[\]]{0,2047}[0-9+/=:@-])"
        r"[^\s,;\"'&(){}\[\]]{4,2048})"
    ),
    rf"\g<label>\g<separator>{REDACTION_MARKER}",
)
_SECRET_PATTERNS: tuple[_SecretPattern, ...] = (
    _PRIVATE_KEY,
    *_COMMON_TOKEN_PATTERNS,
    _AUTHORIZATION_HEADER,
    _COOKIE_HEADER,
    _NAMED_QUOTED_SECRET,
    _NAMED_UNQUOTED_SECRET,
    _CREDENTIAL_URL,
    _QUERY_SECRET,
    _BARE_BEARER,
)


def _text_and_size(value: str | bytes) -> tuple[str, int]:
    if isinstance(value, str):
        return value, len(value.encode("utf-8"))
    if isinstance(value, bytes):
        return value.decode("latin-1"), len(value)
    raise TypeError("secret scan input must be str or bytes")


def _require_budget(size: int, max_bytes: int) -> None:
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("secret scan byte budget must be a positive integer")
    if size > max_bytes:
        raise SecretScanLimitError(
            f"secret scan input exceeds the {max_bytes}-byte limit"
        )


def _protected_values(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError("protected_values must be an iterable of strings")
    normalized: set[str] = set()
    total_bytes = 0
    for value in values:
        if not isinstance(value, str):
            raise TypeError("protected_values must contain only strings")
        if value and value != REDACTION_MARKER:
            if value not in normalized:
                total_bytes += len(value.encode("utf-8"))
                if total_bytes > MAX_PROTECTED_VALUE_BYTES:
                    raise SecretScanLimitError(
                        "protected secret values exceed the aggregate byte limit"
                    )
                normalized.add(value)
    return tuple(sorted(normalized, key=lambda item: (-len(item), item)))


def secret_kinds(
    value: str | bytes,
    *,
    protected_values: Iterable[str] = (),
    max_bytes: int = MAX_SECRET_SCAN_BYTES,
) -> tuple[str, ...]:
    """Return deterministic secret categories without exposing matched material."""
    text, size = _text_and_size(value)
    _require_budget(size, max_bytes)
    kinds: set[str] = set()
    if any(secret in text for secret in _protected_values(protected_values)):
        kinds.add("protected_value")
    for pattern in _SECRET_PATTERNS:
        if pattern.expression.search(text):
            kinds.add(pattern.kind)
    return tuple(sorted(kinds))


def contains_secret(
    value: str | bytes,
    *,
    protected_values: Iterable[str] = (),
    max_bytes: int = MAX_SECRET_SCAN_BYTES,
) -> bool:
    """Return whether bounded text or bytes contain canonical secret material."""
    return bool(
        secret_kinds(
            value,
            protected_values=protected_values,
            max_bytes=max_bytes,
        )
    )


def redact_secrets(
    value: str,
    *,
    protected_values: Iterable[str] = (),
    max_bytes: int = MAX_SECRET_SCAN_BYTES,
) -> tuple[str, bool]:
    """Redact canonical secret values while preserving non-secret context."""
    if not isinstance(value, str):
        raise TypeError("secret redaction input must be str")
    _, size = _text_and_size(value)
    _require_budget(size, max_bytes)
    redacted = False
    for secret in _protected_values(protected_values):
        if secret in value:
            value = value.replace(secret, REDACTION_MARKER)
            redacted = True
    for pattern in _SECRET_PATTERNS:
        value, count = pattern.expression.subn(pattern.replacement, value)
        redacted = redacted or count > 0
    return value, redacted


def structured_value_contains_secret(
    value: Any,
    *,
    protected_values: Iterable[str] = (),
    max_text_bytes: int = MAX_SECRET_SCAN_BYTES,
    max_nodes: int = MAX_STRUCTURED_SCAN_NODES,
) -> bool:
    """Scan a JSON-shaped value with aggregate byte and node work ceilings."""
    if type(max_nodes) is not int or max_nodes <= 0:
        raise ValueError("structured secret scan node budget must be positive")
    if type(max_text_bytes) is not int or max_text_bytes <= 0:
        raise ValueError("structured secret scan byte budget must be positive")
    protected = _protected_values(protected_values)
    stack = [value]
    nodes = 0
    text_bytes = 0
    seen_containers: set[int] = set()
    while stack:
        item = stack.pop()
        nodes += 1
        if nodes > max_nodes:
            raise SecretScanLimitError(
                f"structured secret scan exceeds the {max_nodes}-node limit"
            )
        if isinstance(item, str):
            text_bytes += len(item.encode("utf-8"))
            if text_bytes > max_text_bytes:
                raise SecretScanLimitError(
                    "structured secret scan exceeds the aggregate text byte limit"
                )
            if contains_secret(
                item,
                protected_values=protected,
                max_bytes=max_text_bytes,
            ):
                return True
            continue
        if isinstance(item, Mapping):
            identity = id(item)
            if identity in seen_containers:
                continue
            seen_containers.add(identity)
            for key, child in item.items():
                if not isinstance(key, str):
                    raise TypeError("structured secret scan mappings require string keys")
                stack.extend((key, child))
            continue
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            identity = id(item)
            if identity in seen_containers:
                continue
            seen_containers.add(identity)
            stack.extend(item)
            continue
        if item is None or isinstance(item, (bool, int, float)):
            continue
        raise TypeError("structured secret scan accepts only JSON-shaped values")
    return False
