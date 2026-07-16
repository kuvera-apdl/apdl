import pytest

from app.service_auth import service_headers


def test_service_headers_uses_project_map(monkeypatch):
    monkeypatch.setenv(
        "APDL_SERVICE_API_KEYS",
        '{"acme":"proj_acme_0123456789abcdef"}',
    )
    monkeypatch.delenv("APDL_DEV_API_KEY", raising=False)

    assert service_headers("acme") == {"X-API-Key": "proj_acme_0123456789abcdef"}


def test_service_headers_reuses_single_local_development_key(monkeypatch):
    monkeypatch.delenv("APDL_SERVICE_API_KEYS", raising=False)
    monkeypatch.setenv(
        "APDL_DEV_API_KEY",
        "proj_acme_0123456789abcdef",
    )

    assert service_headers("acme") == {"X-API-Key": "proj_acme_0123456789abcdef"}


def test_service_headers_rejects_local_key_for_another_project(monkeypatch):
    monkeypatch.delenv("APDL_SERVICE_API_KEYS", raising=False)
    monkeypatch.setenv(
        "APDL_DEV_API_KEY",
        "proj_other_0123456789abcdef",
    )

    with pytest.raises(RuntimeError, match="No service API key"):
        service_headers("acme")


def test_service_headers_rejects_malformed_project_map(monkeypatch):
    monkeypatch.setenv("APDL_SERVICE_API_KEYS", "not-json")

    with pytest.raises(RuntimeError, match="must be a JSON object"):
        service_headers("acme")


@pytest.mark.parametrize(
    "keys_json",
    [
        '{"acme":42}',
        '{"acme":"proj_other_0123456789abcdef"}',
        '{"acme":"malformed"}',
    ],
)
def test_service_headers_rejects_invalid_project_key(monkeypatch, keys_json):
    monkeypatch.setenv("APDL_SERVICE_API_KEYS", keys_json)

    with pytest.raises(RuntimeError, match="must be a valid key for that project"):
        service_headers("acme")
