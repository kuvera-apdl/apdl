"""Outer ASGI request-body boundary shared by APDL HTTP services."""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

DEFAULT_MAX_REQUEST_BODY_BYTES = 2 * 1024 * 1024
PAYLOAD_TOO_LARGE_CONTENT = {
    "error": "payload_too_large",
    "message": "Request body exceeds the configured limit",
}
INVALID_CONTENT_LENGTH_CONTENT = {
    "error": "bad_request",
    "message": "Invalid Content-Length",
}
_PAYLOAD_TOO_LARGE_BODY = (
    b'{"error":"payload_too_large",'
    b'"message":"Request body exceeds the configured limit"}'
)
_INVALID_CONTENT_LENGTH_BODY = (
    b'{"error":"bad_request","message":"Invalid Content-Length"}'
)


class RequestBodyTooLarge(Exception):
    """The observed request stream crossed its configured byte ceiling."""


def _declared_content_length(scope: Scope) -> int | None:
    values = [
        value
        for name, value in scope.get("headers", ())
        if name.lower() == b"content-length"
    ]
    if not values:
        return None
    if len(values) != 1:
        raise ValueError("duplicate Content-Length")
    raw = values[0]
    if not raw or any(byte < ord("0") or byte > ord("9") for byte in raw):
        raise ValueError("invalid Content-Length")
    return int(raw)


async def _send_json(
    send: Send,
    *,
    status: int,
    body: bytes,
) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"no-store"),
                (b"x-content-type-options", b"nosniff"),
            ],
        }
    )
    await send(
        {
            "type": "http.response.body",
            "body": body,
            "more_body": False,
        }
    )


class RequestBodyLimitMiddleware:
    """Reject declared or streamed request bodies before they can be buffered."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_body_bytes: int = DEFAULT_MAX_REQUEST_BODY_BYTES,
    ) -> None:
        if type(max_body_bytes) is not int or max_body_bytes < 1:
            raise ValueError("max_body_bytes must be a positive integer")
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        try:
            declared_bytes = _declared_content_length(scope)
        except ValueError:
            await _send_json(
                send,
                status=400,
                body=_INVALID_CONTENT_LENGTH_BODY,
            )
            return
        if (
            declared_bytes is not None
            and declared_bytes > self.max_body_bytes
        ):
            await _send_json(
                send,
                status=413,
                body=_PAYLOAD_TOO_LARGE_BODY,
            )
            return

        observed_bytes = 0
        response_started = False

        async def receive_bounded() -> Message:
            nonlocal observed_bytes
            message = await receive()
            if message["type"] == "http.request":
                observed_bytes += len(message.get("body", b""))
                if observed_bytes > self.max_body_bytes:
                    raise RequestBodyTooLarge
            return message

        async def send_tracked(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive_bounded, send_tracked)
        except RequestBodyTooLarge:
            if response_started:
                raise RuntimeError(
                    "request body crossed its limit after response start"
                ) from None
            await _send_json(
                send,
                status=413,
                body=_PAYLOAD_TOO_LARGE_BODY,
            )
