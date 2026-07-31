"""Duplicate-key and non-finite JSON rejection for secret-bearing API bodies."""

from __future__ import annotations

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send


_INVALID_JSON = b'{"detail":"Request body must be one strict JSON object"}'
_BODY_TOO_LARGE = b'{"detail":"Request body exceeds the connection API limit"}'
_MAX_CONNECTION_BODY_BYTES = 32 * 1024


class StrictConnectionJsonMiddleware:
    """Validate connection mutation JSON before framework key collapsing."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") not in {"PUT", "POST"}
            or not str(scope.get("path", "")).startswith("/v1/llm-connections/")
        ):
            await self.app(scope, receive, send)
            return
        messages: list[Message] = []
        body = bytearray()
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] == "http.request":
                body.extend(message.get("body", b""))
                if len(body) > _MAX_CONNECTION_BODY_BYTES:
                    await send(
                        {
                            "type": "http.response.start",
                            "status": 413,
                            "headers": [
                                (b"content-type", b"application/json"),
                                (
                                    b"content-length",
                                    str(len(_BODY_TOO_LARGE)).encode(),
                                ),
                                (b"cache-control", b"no-store"),
                            ],
                        }
                    )
                    await send(
                        {
                            "type": "http.response.body",
                            "body": _BODY_TOO_LARGE,
                            "more_body": False,
                        }
                    )
                    return
                if not message.get("more_body", False):
                    break
            elif message["type"] == "http.disconnect":
                await self.app(scope, receive, send)
                return

        def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in values:
                if key in result:
                    raise ValueError("duplicate key")
                result[key] = value
            return result

        def reject_nonfinite(_value: str) -> None:
            raise ValueError("non-finite value")

        try:
            decoded = body.decode("utf-8", errors="strict")
            payload = json.loads(
                decoded,
                object_pairs_hook=pairs,
                parse_constant=reject_nonfinite,
            )
            if not isinstance(payload, dict):
                raise ValueError("body is not an object")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            await send(
                {
                    "type": "http.response.start",
                    "status": 400,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(_INVALID_JSON)).encode()),
                        (b"cache-control", b"no-store"),
                    ],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": _INVALID_JSON,
                    "more_body": False,
                }
            )
            return

        index = 0

        async def replay() -> Message:
            nonlocal index
            if index < len(messages):
                message = messages[index]
                index += 1
                return message
            return {"type": "http.disconnect"}

        await self.app(scope, replay, send)
