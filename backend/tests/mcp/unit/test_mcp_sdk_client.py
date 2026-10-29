"""In-process official MCP SDK client tests.

Verifies protocol initialization, discovery, tool invocation, and error handling
using the official Python SDK (mcp==2.3.0) and in-memory streams.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import anyio
import mcp.types as types
import pytest
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from app.contracts.search import (
    SearchMode,
    SearchResponseV1,
    SearchScopeV1,
)
from app.contracts.source_view import (
    ByteRangeV1,
    SourceRepresentation,
    SourceViewResultV1,
)
from app.domain.scope import OrgRole, TenantContext
from app.mcp.server import ReadOnlyMcpServer, create_mcp_server
from mcp.client.session import ClientSession


@pytest.fixture
def mock_tenant() -> TenantContext:
    return TenantContext(
        org_id=uuid4(),
        principal_id=uuid4(),
        role=OrgRole.MEMBER,
    )


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
async def test_sdk_initialize_and_discovery(mock_tenant: TenantContext) -> None:
    facade = ReadOnlyMcpServer(identity=mock_tenant)
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
            init_res = await session.initialize()
            assert init_res.server_info.name == "codespy-retrieval-mcp"
            assert init_res.server_info.version == "1.0.0"

            tools_res = await session.list_tools()
            tool_names = {t.name for t in tools_res.tools}
            expected_names = {
                "get_index_status",
                "get_source_view",
                "search_exact",
                "search_hybrid",
                "assemble_context",
                "query_memory",
                "get_investigation_status",
                "get_investigation_result",
                "list_findings",
                "get_evidence",
                "graph_neighbors",
                "trace_feature",
                "list_claims",
            }
            assert tool_names == expected_names
            assert len(tools_res.tools) == 13

            # Verify input_schema properties exist on all tools
            for tool in tools_res.tools:
                assert tool.description
                assert tool.input_schema is not None
                assert tool.input_schema["type"] == "object"
                assert "properties" in tool.input_schema
                assert "repository_id" in tool.input_schema["properties"]

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_sdk_call_tool_unknown_tool(mock_tenant: TenantContext) -> None:
    facade = ReadOnlyMcpServer(identity=mock_tenant)
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
            res = await session.call_tool("non_existent_tool", {})
            assert res.is_error is True
            assert isinstance(res.content[0], types.TextContent)
            assert "Invalid Parameters" in res.content[0].text or "Unknown" in res.content[0].text

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_sdk_call_tool_forbidden_identity_injection(mock_tenant: TenantContext) -> None:
    facade = ReadOnlyMcpServer(identity=mock_tenant)
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
            # Try to forge identity via arguments
            res = await session.call_tool(
                "get_index_status",
                {
                    "repository_id": str(uuid4()),
                    "snapshot_id": str(uuid4()),
                    "job_id": str(uuid4()),
                    "org_id": str(uuid4()),
                },
            )
            assert res.is_error is True
            assert isinstance(res.content[0], types.TextContent)
            assert "Security/Scope Violation" in res.content[0].text

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_sdk_call_tool_repository_allowlist(mock_tenant: TenantContext) -> None:
    allowed_repo = uuid4()
    disallowed_repo = uuid4()
    facade = ReadOnlyMcpServer(
        identity=mock_tenant,
        repository_allowlist=frozenset([allowed_repo]),
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

            # Disallowed repository must be rejected
            denied_res = await session.call_tool(
                "get_index_status",
                {
                    "repository_id": str(disallowed_repo),
                    "snapshot_id": str(uuid4()),
                    "job_id": str(uuid4()),
                },
            )
            assert denied_res.is_error is True
            assert isinstance(denied_res.content[0], types.TextContent)
            assert denied_res.structured_content["code"] == "scope_denied"

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_sdk_call_search_exact_dispatch(mock_tenant: TenantContext) -> None:
    repo_id = uuid4()
    snap_id = uuid4()
    mock_lexical = AsyncMock()
    scope = SearchScopeV1(
        org_id=mock_tenant.org_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        principal_id=mock_tenant.principal_id,
    )
    mock_lexical.search_exact.return_value = SearchResponseV1(
        scope=scope,
        mode=SearchMode.EXACT,
        query="fn main",
        total_candidates=1,
        hits=(),
    )
    facade = ReadOnlyMcpServer(
        identity=mock_tenant,
        lexical_service=mock_lexical,
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
                "search_exact",
                {
                    "repository_id": str(repo_id),
                    "snapshot_id": str(snap_id),
                    "query": "fn main",
                },
            )
            assert res.is_error is False
            assert isinstance(res.content[0], types.TextContent)
            payload = json.loads(res.content[0].text)
            assert payload["query"] == "fn main"
            assert payload["mode"] == "exact"
            mock_lexical.search_exact.assert_awaited_once()

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_sdk_call_source_view_dispatch(mock_tenant: TenantContext) -> None:
    repo_id = uuid4()
    snap_id = uuid4()
    file_id = uuid4()
    mock_source_view = AsyncMock()
    mock_source_view.get.return_value = SourceViewResultV1(
        file_id=file_id,
        file_path="src/lib.rs",
        file_sha256="abc1234",
        representation=SourceRepresentation.MODEL_SAFE,
        requested_range=ByteRangeV1(start_byte=0, end_byte=50),
        actual_range=ByteRangeV1(start_byte=0, end_byte=50),
        content_bytes=b"pub fn verify() {}",
        content_text="pub fn verify() {}",
        content_sha256="hash123",
        total_file_size=100,
    )
    facade = ReadOnlyMcpServer(
        identity=mock_tenant,
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
                    "start_byte": 0,
                    "end_byte": 50,
                },
            )
            assert res.is_error is False
            assert isinstance(res.content[0], types.TextContent)
            payload = json.loads(res.content[0].text)
            assert payload["file_path"] == "src/lib.rs"
            assert payload["content_text"] == "pub fn verify() {}"
            mock_source_view.get.assert_awaited_once()

        tg.cancel_scope.cancel()
