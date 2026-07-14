"""Exact-name selection and deterministic contract-cache identity tests."""

import json

from app.contracts.cache import build_cache_identity
from app.contracts.models import ContractRequest, RuntimeFingerprint
from app.contracts.selection import select_contract_requests
from app.profiling import profile_repository


def _write(root, path: str, text: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _runtime(version: str = "20.18.0") -> RuntimeFingerprint:
    return RuntimeFingerprint(
        runtime_name="node",
        runtime_version=version,
        operating_system="linux",
        architecture="x86_64",
    )


def _request() -> ContractRequest:
    return ContractRequest(
        ecosystem="node",
        package_path=".",
        package_name="react-dom",
        exact_version="19.0.0",
        manifest_path="package.json",
        lockfile_path="package-lock.json",
        symbols=["createRoot"],
    )


def test_selection_matches_complete_package_name_without_fuzzy_prefix(tmp_path):
    _write(
        tmp_path,
        "package.json",
        json.dumps(
            {
                "name": "web",
                "dependencies": {"react": "^19", "react-dom": "^19"},
            }
        ),
    )
    _write(
        tmp_path,
        "package-lock.json",
        json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "node_modules/react": {"version": "19.0.0"},
                    "node_modules/react-dom": {"version": "19.0.0"},
                },
            }
        ),
    )
    profile = profile_repository(tmp_path)

    requests = select_contract_requests(
        profile,
        "Use react-dom's createRoot API; do not add a reactive wrapper.",
        requirement_ids=["req-1"],
        symbols_by_package={"react-dom": ["createRoot"]},
    )

    assert [request.package_name for request in requests] == ["react-dom"]
    assert requests[0].exact_version == "19.0.0"
    assert requests[0].symbols == ["createRoot"]


def test_cache_identity_changes_with_lock_manifest_runtime_and_scope(tmp_path):
    _write(tmp_path, "package.json", '{"dependencies":{"react-dom":"^19"}}')
    _write(tmp_path, "package-lock.json", '{"lockfileVersion":3}')
    request = _request()

    def identity(project="project-a", runtime=None):
        return build_cache_identity(
            tmp_path,
            project_scope=project,
            repository="acme/web",
            request=request,
            runtime=runtime or _runtime(),
            extractor_version="extractor@1",
        )

    original = identity()
    assert original == identity()
    assert original.cache_key != identity(project="project-b").cache_key
    assert original.cache_key != identity(runtime=_runtime("22.0.0")).cache_key

    _write(tmp_path, "package-lock.json", '{"lockfileVersion":3,"changed":true}')
    lock_changed = identity()
    assert lock_changed.cache_key != original.cache_key
    assert lock_changed.lockfile_sha256 != original.lockfile_sha256

    _write(
        tmp_path,
        "package.json",
        '{"dependencies":{"react-dom":"19.0.0"},"private":true}',
    )
    manifest_changed = identity()
    assert manifest_changed.cache_key != lock_changed.cache_key
    assert manifest_changed.manifest_sha256 != lock_changed.manifest_sha256


def test_cache_identity_changes_with_requested_symbol_set(tmp_path):
    _write(tmp_path, "package.json", "{}")
    _write(tmp_path, "package-lock.json", "{}")
    one = build_cache_identity(
        tmp_path,
        project_scope="project-a",
        repository="acme/web",
        request=_request(),
        runtime=_runtime(),
        extractor_version="extractor@1",
    )
    two = build_cache_identity(
        tmp_path,
        project_scope="project-a",
        repository="acme/web",
        request=_request().model_copy(update={"symbols": ["hydrateRoot"]}),
        runtime=_runtime(),
        extractor_version="extractor@1",
    )
    assert one.selection_sha256 != two.selection_sha256
    assert one.cache_key != two.cache_key
