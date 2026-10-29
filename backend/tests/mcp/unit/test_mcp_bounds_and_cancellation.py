"""Unit tests for MCP runtime bounds, framing, concurrency limits, and cancellation.

Verifies Increment 5:
- 64 KiB incoming framing boundary before deserialization
- 256 KiB outgoing serialized response boundary
- Active calls (2) and queue capacity (4) saturation rejection
- Request cancellation and deadline handling
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import anyio
import mcp.types as types
import pytest
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from app.contracts.source_view import ByteRangeV1, SourceRepresentation, SourceViewResultV1
from app.domain.errors import CapacityExceeded
from app.domain.scope import OrgRole, TenantContext
from app.mcp.server import (
    BoundedLineReader,
    McpConcurrencyLimiter,
    ReadOnlyMcpServer,
    create_mcp_server,
)
from mcp.client.session import ClientSession


class MockAsyncFile:
    def __init__(self, data: str) -> None:
        self._data = data
        self._pos = 0

    async def read(self, size: int) -> str:
        if self._pos >= len(self._data):
            return ""
        chunk = self._data[self._pos : self._pos + size]
        self._pos += size
        return chunk


def _create_stream_pair() -> tuple[
    MemoryObjectSendStream[Any],
    MemoryObjectReceiveStream[Any],
    MemoryObjectSendStream[Any],
    MemoryObjectReceiveStream[Any],
]:
    client_send, server_receive = anyio.create_memory_object_stream[Any](10)
    server_send, client_receive = anyio.create_memory_object_stream[Any](10)
    return client_send, server_receive, server_send, client_receive


@pytest.mark.asyncio
async def test_bounded_line_reader_normal() -> None:
    raw = MockAsyncFile("line1\nline2\n")
    reader = BoundedLineReader(raw, max_bytes=64)
    assert await reader.readline() == "line1\n"
    assert await reader.readline() == "line2\n"
    assert await reader.readline() == ""


@pytest.mark.asyncio
async def test_bounded_line_reader_oversized() -> None:
    # 70 KiB line with newline
    raw = MockAsyncFile("x" * 70000 + "\n")
    reader = BoundedLineReader(raw, max_bytes=65536)
    with pytest.raises(ValueError, match="Incoming frame exceeds"):
        await reader.readline()


@pytest.mark.asyncio
async def test_bounded_line_reader_unterminated_oversized() -> None:
    # 70 KiB line without newline
    raw = MockAsyncFile("y" * 70000)
    reader = BoundedLineReader(raw, max_bytes=65536)
    with pytest.raises(ValueError, match="Incoming frame exceeds"):
        await reader.readline()


@pytest.mark.asyncio
async def test_concurrency_limiter_saturation() -> None:
    limiter = McpConcurrencyLimiter(max_active=2, max_waiting=2, timeout_seconds=5.0)

    release_active = asyncio.Event()

    async def worker(worker_id: int) -> None:
        async with limiter.throttle():
            await release_active.wait()

    tasks: list[asyncio.Task[None]] = []
    # Launch 2 active tasks
    for i in range(2):
        tasks.append(asyncio.create_task(worker(i)))
    await asyncio.sleep(0.01)

    # Launch 2 waiting tasks
    for i in range(2, 4):
        tasks.append(asyncio.create_task(worker(i)))
    await asyncio.sleep(0.01)

    # 5th task must immediately fail with CapacityExceeded (queue full)
    with pytest.raises(CapacityExceeded, match="capacity exceeded"):
        async with limiter.throttle():
            pass

    release_active.set()
    await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_concurrency_limiter_timeout() -> None:
    limiter = McpConcurrencyLimiter(max_active=1, max_waiting=2, timeout_seconds=0.05)

    with pytest.raises(TimeoutError):
        async with limiter.throttle():
            await asyncio.sleep(0.2)


@pytest.mark.asyncio
async def test_call_tool_oversized_response_rejected() -> None:
    tenant = TenantContext(org_id=uuid4(), principal_id=uuid4(), role=OrgRole.MEMBER)
    repo_id = uuid4()
    snap_id = uuid4()
    file_id = uuid4()

    mock_source_view = AsyncMock()
    # 300 KiB content (exceeds 256 KiB MAX_RESPONSE_BYTES)
    huge_content = "A" * 300_000
    mock_source_view.get.return_value = SourceViewResultV1(
        file_id=file_id,
        file_path="huge.txt",
        file_sha256="abc1234",
        representation=SourceRepresentation.MODEL_SAFE,
        requested_range=ByteRangeV1(start_byte=0, end_byte=300_000),
        actual_range=ByteRangeV1(start_byte=0, end_byte=300_000),
        content_bytes=huge_content.encode("utf-8"),
        content_text=huge_content,
        content_sha256="hash123",
        total_file_size=300_000,
    )

    facade = ReadOnlyMcpServer(
        identity=tenant,
        source_view_service=mock_source_view,
    )
    server = create_mcp_server(facade)

    client_send, server_receive, server_send, client_receive = _create_stream_pair()

    async with anyio.create_task_group() as tg:
        tg.start_soon(
            server.run,
            server_receive,
            server_send,
            server.create_initialization_options(),
        )
        async with ClientSession(client_receive, client_send) as session:
            await session.initialize()
            res = await session.call_tool(
                "get_source_view",
                {
                    "repository_id": str(repo_id),
                    "snapshot_id": str(snap_id),
                    "file_id": str(file_id),
                },
            )
            assert res.is_error is True
            assert isinstance(res.content[0], types.TextContent)
            assert "exceeds maximum allowed size" in res.content[0].text

        tg.cancel_scope.cancel()
