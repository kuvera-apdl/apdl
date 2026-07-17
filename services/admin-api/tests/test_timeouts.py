from __future__ import annotations

import asyncio

import httpx
import pytest

from app.main import _upstream_timeout
from conftest import make_settings


def test_upstream_timeout_keeps_sse_reads_above_the_heartbeat_window() -> None:
    timeout = _upstream_timeout(make_settings())

    assert timeout.connect == 5.0
    assert timeout.read == 60.0
    assert timeout.write == 30.0
    assert timeout.pool == 30.0


@pytest.mark.asyncio
async def test_stream_read_timeout_survives_heartbeat_jitter_for_its_full_lifetime() -> (
    None
):
    heartbeat = b"event: heartbeat\ndata: {}\n\n"

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/event-stream\r\n"
                b"Transfer-Encoding: chunked\r\n"
                b"Connection: close\r\n\r\n"
            )
            await writer.drain()
            for delay in (0.06, 0.12, 0.18):
                await asyncio.sleep(delay)
                writer.write(
                    f"{len(heartbeat):X}\r\n".encode("ascii") + heartbeat + b"\r\n"
                )
                await writer.drain()
            writer.write(b"0\r\n\r\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    socket = server.sockets[0]
    host, port = socket.getsockname()[:2]
    settings = make_settings(upstream_read_timeout_seconds=0.3)

    async with server:
        async with httpx.AsyncClient(timeout=_upstream_timeout(settings)) as client:
            response = await client.get(f"http://{host}:{port}/stream")

    assert response.status_code == 200
    assert response.content.count(heartbeat) == 3
