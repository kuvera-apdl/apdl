from pathlib import Path


def test_public_gateway_overwrites_client_forwarding_headers():
    repo_root = Path(__file__).resolve().parents[3]
    config = (repo_root / "infra/docker/gateway/nginx.conf").read_text()
    ingestion_location = config.split("location = /v1/events {", 1)[1].split(
        "\n    }",
        1,
    )[0]

    assert "$proxy_add_x_forwarded_for" not in config
    assert "proxy_set_header Forwarded \"\";" in ingestion_location
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in ingestion_location
    assert "proxy_set_header X-Real-IP \"\";" in ingestion_location


def test_ingestion_runtime_leaves_socket_peer_authority_to_application_policy():
    repo_root = Path(__file__).resolve().parents[3]
    dockerfile = (repo_root / "services/ingestion/Dockerfile").read_text()
    makefile = (repo_root / "Makefile").read_text()

    assert '"--no-proxy-headers"' in dockerfile
    run_ingestion = makefile.split("run-ingestion:", 1)[1].split("\n\n", 1)[0]
    assert "--no-proxy-headers" in run_ingestion
