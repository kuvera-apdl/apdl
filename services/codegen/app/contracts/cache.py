"""Content-addressed cache primitives for dependency contracts."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path, PurePosixPath
from typing import Protocol

from app.contracts.models import (
    ContractCacheIdentity,
    ContractRequest,
    ContractResolution,
    RuntimeFingerprint,
)


class CacheIdentityError(ValueError):
    """A manifest or lockfile cannot form a safe cache identity."""


class CacheCorruptionError(ValueError):
    """Cached JSON failed strict schema validation."""


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return sha256_bytes(encoded)


def _repo_file(root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or not pure.parts or ".." in pure.parts:
        raise CacheIdentityError(f"Unsafe repository path: {relative!r}")
    candidate = root.joinpath(*pure.parts)
    if not candidate.is_file():
        raise CacheIdentityError(f"Repository file does not exist: {relative}")
    return candidate


def build_cache_identity(
    repository_root: Path,
    *,
    project_scope: str,
    repository: str,
    request: ContractRequest,
    runtime: RuntimeFingerprint,
    extractor_version: str,
) -> ContractCacheIdentity:
    """Build a deterministic repo-scoped lock/manifest/runtime cache identity."""
    if request.lockfile_path is None:
        raise CacheIdentityError("An exact contract cache requires a lockfile")
    manifest = _repo_file(repository_root, request.manifest_path)
    lockfile = _repo_file(repository_root, request.lockfile_path)
    manifest_sha = sha256_file(manifest)
    lockfile_sha = sha256_file(lockfile)
    selection_sha = canonical_sha256(
        {
            "ecosystem": request.ecosystem,
            "package_path": request.package_path,
            "package_name": request.package_name,
            "exact_version": request.exact_version,
            "symbols": request.symbols,
        }
    )
    payload = {
        "schema_version": "contract_cache_identity@1",
        "project_scope": project_scope,
        "repository": repository,
        "ecosystem": request.ecosystem,
        "package_path": request.package_path,
        "manifest_path": request.manifest_path,
        "manifest_sha256": manifest_sha,
        "lockfile_path": request.lockfile_path,
        "lockfile_sha256": lockfile_sha,
        "runtime": runtime.model_dump(mode="json"),
        "extractor_version": extractor_version,
        "selection_sha256": selection_sha,
    }
    return ContractCacheIdentity(**payload, cache_key=canonical_sha256(payload))


class ContractCache(Protocol):
    def get(self, identity: ContractCacheIdentity) -> ContractResolution | None: ...

    def put(
        self, identity: ContractCacheIdentity, resolution: ContractResolution
    ) -> None: ...


def _encode_resolution(resolution: ContractResolution) -> str:
    return json.dumps(
        resolution.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _decode_resolution(raw: str, expected_key: str) -> ContractResolution:
    try:
        resolution = ContractResolution.model_validate_json(raw)
    except Exception as exc:
        raise CacheCorruptionError("Cached contract resolution is invalid") from exc
    identity = resolution.cache_identity
    if identity is None or identity.cache_key != expected_key:
        raise CacheCorruptionError("Cached contract identity does not match its key")
    return resolution


class MemoryContractCache:
    """Small strict cache useful for a worker lifetime and unit tests."""

    def __init__(self) -> None:
        self._entries: dict[str, str] = {}

    def get(self, identity: ContractCacheIdentity) -> ContractResolution | None:
        raw = self._entries.get(identity.cache_key)
        return None if raw is None else _decode_resolution(raw, identity.cache_key)

    def put(
        self, identity: ContractCacheIdentity, resolution: ContractResolution
    ) -> None:
        if resolution.cache_identity != identity:
            raise ValueError("Resolution cache identity does not match the cache key")
        self._entries[identity.cache_key] = _encode_resolution(resolution)


class FilesystemContractCache:
    """Atomic strict JSON cache; callers choose a tenant-appropriate directory."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def _path(self, key: str) -> Path:
        if len(key) != 64 or any(char not in "0123456789abcdef" for char in key):
            raise ValueError("Contract cache key must be a SHA-256 hex digest")
        return self._root / f"{key}.json"

    def get(self, identity: ContractCacheIdentity) -> ContractResolution | None:
        path = self._path(identity.cache_key)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        return _decode_resolution(raw, identity.cache_key)

    def put(
        self, identity: ContractCacheIdentity, resolution: ContractResolution
    ) -> None:
        if resolution.cache_identity != identity:
            raise ValueError("Resolution cache identity does not match the cache key")
        self._root.mkdir(parents=True, exist_ok=True)
        target = self._path(identity.cache_key)
        fd, temp_name = tempfile.mkstemp(prefix=".contract-", dir=self._root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(_encode_resolution(resolution))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
