"""Cross-service contracts for the outer ASGI request-body boundary."""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SERVICE_NAMES = (
    "admin-api",
    "agents",
    "codegen",
    "config",
    "ingestion",
    "query",
)
LIMITER_PATHS = tuple(
    ROOT / "services" / service / "app" / "request_body_limit.py"
    for service in SERVICE_NAMES
)
CANONICAL_413 = {
    "error": "payload_too_large",
    "message": "Request body exceeds the configured limit",
}
CANONICAL_400 = {
    "error": "bad_request",
    "message": "Invalid Content-Length",
}


def _load_limiter(path: Path) -> ModuleType:
    """Load a limiter without requiring Starlette in the script-test runtime."""

    starlette = ModuleType("starlette")
    starlette.__path__ = []  # type: ignore[attr-defined]
    starlette_types = ModuleType("starlette.types")
    for name in ("ASGIApp", "Message", "Receive", "Scope", "Send"):
        setattr(starlette_types, name, object)
    starlette.types = starlette_types  # type: ignore[attr-defined]

    module_name = f"_request_body_limit_{path.parents[1].name.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(
        sys.modules,
        {
            "starlette": starlette,
            "starlette.types": starlette_types,
        },
    ):
        spec.loader.exec_module(module)
    return module


async def _request(
    module: ModuleType,
    *,
    headers: tuple[tuple[bytes, bytes], ...] = (),
    chunks: tuple[bytes, ...] = (b"",),
    max_body_bytes: int = 8,
) -> tuple[list[dict[str, object]], bool, int, bytes]:
    messages = [
        {
            "type": "http.request",
            "body": chunk,
            "more_body": index < len(chunks) - 1,
        }
        for index, chunk in enumerate(chunks)
    ]
    received = 0
    app_called = False
    app_body = bytearray()
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        nonlocal received
        if received >= len(messages):
            raise AssertionError("application read past the end of the request")
        message = messages[received]
        received += 1
        return message

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    async def app(
        scope: dict[str, object],
        bounded_receive: object,
        bounded_send: object,
    ) -> None:
        nonlocal app_called
        del scope
        app_called = True
        while True:
            message = await bounded_receive()  # type: ignore[operator]
            app_body.extend(message.get("body", b""))
            if not message.get("more_body", False):
                break
        await bounded_send(  # type: ignore[operator]
            {
                "type": "http.response.start",
                "status": 204,
                "headers": (),
            }
        )
        await bounded_send(  # type: ignore[operator]
            {
                "type": "http.response.body",
                "body": b"",
                "more_body": False,
            }
        )

    middleware = module.RequestBodyLimitMiddleware(
        app,
        max_body_bytes=max_body_bytes,
    )
    await middleware(
        {
            "type": "http",
            "headers": headers,
        },
        receive,
        send,
    )
    return sent, app_called, received, bytes(app_body)


def _response(sent: list[dict[str, object]]) -> tuple[int, dict[str, str]]:
    start, body = sent
    if start["type"] != "http.response.start":
        raise AssertionError(f"unexpected response start: {start}")
    if body["type"] != "http.response.body":
        raise AssertionError(f"unexpected response body: {body}")
    raw_body = body.get("body", b"")
    if not isinstance(raw_body, bytes):
        raise AssertionError(f"response body is not bytes: {raw_body!r}")
    return int(start["status"]), json.loads(raw_body)


class RequestBodyLimitContractTests(unittest.TestCase):
    def test_all_services_ship_the_byte_identical_limiter(self) -> None:
        implementations = [path.read_bytes() for path in LIMITER_PATHS]

        self.assertEqual(len(implementations), len(SERVICE_NAMES))
        for path, implementation in zip(
            LIMITER_PATHS[1:],
            implementations[1:],
            strict=True,
        ):
            with self.subTest(path=path):
                self.assertEqual(implementation, implementations[0])

    def test_every_service_installs_the_limiter_in_main(self) -> None:
        for service in SERVICE_NAMES:
            main_path = ROOT / "services" / service / "app" / "main.py"
            tree = ast.parse(main_path.read_text(encoding="utf-8"))
            imports_limiter = any(
                isinstance(node, ast.ImportFrom)
                and node.module == "app.request_body_limit"
                and any(
                    alias.name == "RequestBodyLimitMiddleware"
                    for alias in node.names
                )
                for node in ast.walk(tree)
            )
            installs_limiter = any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_middleware"
                and bool(node.args)
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "RequestBodyLimitMiddleware"
                for node in ast.walk(tree)
            )

            with self.subTest(service=service):
                self.assertTrue(imports_limiter)
                self.assertTrue(installs_limiter)

    def test_declared_oversized_body_is_rejected_before_application_read(
        self,
    ) -> None:
        for path in LIMITER_PATHS:
            module = _load_limiter(path)
            sent, app_called, received, _ = asyncio.run(
                _request(
                    module,
                    headers=((b"content-length", b"9"),),
                    chunks=(b"ignored",),
                )
            )

            with self.subTest(path=path):
                self.assertEqual(_response(sent), (413, CANONICAL_413))
                self.assertFalse(app_called)
                self.assertEqual(received, 0)

    def test_missing_content_length_is_bounded_by_observed_bytes(self) -> None:
        for path in LIMITER_PATHS:
            module = _load_limiter(path)
            sent, app_called, received, body = asyncio.run(
                _request(module, chunks=(b"1234", b"5678"))
            )

            with self.subTest(path=path):
                self.assertEqual(sent[0]["status"], 204)
                self.assertTrue(app_called)
                self.assertEqual(received, 2)
                self.assertEqual(body, b"12345678")

    def test_oversized_missing_content_length_is_rejected(self) -> None:
        for path in LIMITER_PATHS:
            module = _load_limiter(path)
            sent, app_called, received, body = asyncio.run(
                _request(
                    module,
                    chunks=(b"1234", b"5678", b"9"),
                )
            )

            with self.subTest(path=path):
                self.assertEqual(_response(sent), (413, CANONICAL_413))
                self.assertTrue(app_called)
                self.assertEqual(received, 3)
                self.assertEqual(body, b"12345678")

    def test_oversized_chunked_body_is_rejected_from_the_receive_stream(
        self,
    ) -> None:
        for path in LIMITER_PATHS:
            module = _load_limiter(path)
            sent, app_called, received, body = asyncio.run(
                _request(
                    module,
                    headers=((b"transfer-encoding", b"chunked"),),
                    chunks=(b"1234", b"5678", b"9"),
                )
            )

            with self.subTest(path=path):
                self.assertEqual(_response(sent), (413, CANONICAL_413))
                self.assertTrue(app_called)
                self.assertEqual(received, 3)
                self.assertEqual(body, b"12345678")

    def test_falsified_low_content_length_cannot_bypass_stream_limit(
        self,
    ) -> None:
        for path in LIMITER_PATHS:
            module = _load_limiter(path)
            sent, app_called, received, body = asyncio.run(
                _request(
                    module,
                    headers=((b"content-length", b"1"),),
                    chunks=(b"1234", b"5678", b"9"),
                )
            )

            with self.subTest(path=path):
                self.assertEqual(_response(sent), (413, CANONICAL_413))
                self.assertTrue(app_called)
                self.assertEqual(received, 3)
                self.assertEqual(body, b"12345678")

    def test_exact_limit_is_accepted(self) -> None:
        for path in LIMITER_PATHS:
            module = _load_limiter(path)
            sent, app_called, received, body = asyncio.run(
                _request(
                    module,
                    headers=((b"content-length", b"8"),),
                    chunks=(b"123", b"45678"),
                )
            )

            with self.subTest(path=path):
                self.assertEqual(sent[0]["status"], 204)
                self.assertTrue(app_called)
                self.assertEqual(received, 2)
                self.assertEqual(body, b"12345678")

    def test_invalid_or_duplicate_content_length_is_rejected(self) -> None:
        header_cases = {
            "empty": ((b"content-length", b""),),
            "non_decimal": ((b"content-length", b"eight"),),
            "signed": ((b"content-length", b"+8"),),
            "whitespace": ((b"content-length", b" 8"),),
            "duplicate": (
                (b"content-length", b"8"),
                (b"content-length", b"8"),
            ),
        }
        for path in LIMITER_PATHS:
            module = _load_limiter(path)
            for case, headers in header_cases.items():
                sent, app_called, received, _ = asyncio.run(
                    _request(module, headers=headers, chunks=(b"12345678",))
                )

                with self.subTest(path=path, case=case):
                    self.assertEqual(_response(sent), (400, CANONICAL_400))
                    self.assertFalse(app_called)
                    self.assertEqual(received, 0)


class NginxRequestBodyLimitContractTests(unittest.TestCase):
    def test_public_gateway_matches_the_ingestion_512_kib_limit(self) -> None:
        config = (
            ROOT / "infra" / "docker" / "gateway" / "nginx.conf"
        ).read_text(encoding="utf-8")
        events_location = config.split(
            "location = /v1/events {",
            1,
        )[1].split("\n    }", 1)[0]
        error_location = config.split(
            "location @payload_too_large {",
            1,
        )[1].split("\n    }", 1)[0]

        self.assertIn("client_max_body_size 512k;", events_location)
        self.assertIn(
            "error_page 413 = @payload_too_large;",
            events_location,
        )
        self._assert_canonical_413_location(error_location)

    def test_admin_edge_matches_the_admin_api_2_mib_limit(self) -> None:
        config = (ROOT / "services" / "admin" / "nginx.conf").read_text(
            encoding="utf-8"
        )
        server_prefix = config.split("location ", 1)[0]
        auth_location = config.split(
            "location ~ ^/api/auth/(login|register)$ {",
            1,
        )[1].split("\n    }", 1)[0]
        api_location = config.split(
            "location /api/ {",
            1,
        )[1].split("\n    }", 1)[0]
        error_location = config.split(
            "location @payload_too_large {",
            1,
        )[1].split("\n    }", 1)[0]

        self.assertIn("client_max_body_size 2m;", server_prefix)
        self.assertIn(
            "error_page 413 = @payload_too_large;",
            server_prefix,
        )
        self.assertIn(
            "error_page 413 = @payload_too_large;",
            auth_location,
        )
        self.assertIn(
            "error_page 413 = @payload_too_large;",
            api_location,
        )
        self._assert_canonical_413_location(error_location)

    def _assert_canonical_413_location(self, location: str) -> None:
        canonical_json = json.dumps(CANONICAL_413, separators=(",", ":"))

        self.assertIn("internal;", location)
        self.assertIn("default_type application/json;", location)
        self.assertIn('add_header Cache-Control "no-store" always;', location)
        self.assertIn(
            'add_header X-Content-Type-Options "nosniff" always;',
            location,
        )
        self.assertIn(f"return 413 '{canonical_json}';", location)


if __name__ == "__main__":
    unittest.main()
