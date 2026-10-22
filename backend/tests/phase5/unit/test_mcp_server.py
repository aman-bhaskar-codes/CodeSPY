import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from app.contracts.chunking import ChunkKind
from app.contracts.context import (
    ContextItemV1,
    ContextPacketV1,
)
from app.contracts.memory import (
    MemoryEntryV1,
    MemoryQueryResponseV1,
)
from app.contracts.search import (
    HybridSearchHitV1,
    HybridSearchResponseV1,
    SearchMode,
    SearchResponseV1,
    SearchScopeV1,
)
from app.contracts.source_view import (
    ByteRangeV1,
    SourceRepresentation,
    SourceViewResultV1,
)
from app.domain.errors import ScopeViolation
from app.mcp.server import ReadOnlyMcpServer


@pytest.mark.asyncio
async def test_mcp_protocol_handshake_and_tools_list() -> None:
    server = ReadOnlyMcpServer()

    # 1. Initialize
    init_req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "clientInfo": {"name": "test-client", "version": "1.0"},
        },
    }
    init_res = await server.handle_request(init_req)
    assert init_res["jsonrpc"] == "2.0"
    assert init_res["id"] == 1
    assert init_res["result"]["serverInfo"]["name"] == ReadOnlyMcpServer.SERVER_NAME
    assert init_res["result"]["protocolVersion"] == "2024-11-05"

    # 2. Initialized notification
    notif_res = await server.handle_request(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    assert notif_res["jsonrpc"] == "2.0"

    # 3. List tools
    list_res = await server.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert list_res["id"] == 2
    tools = list_res["result"]["tools"]
    tool_names = [t["name"] for t in tools]
    assert "get_source_view" in tool_names
    assert "search_exact" in tool_names
    assert "search_hybrid" in tool_names
    assert "assemble_context" in tool_names
    assert "query_memory" in tool_names

    # 4. Unknown method
    unknown_res = await server.handle_request(
        {"jsonrpc": "2.0", "id": 3, "method": "unsupported/method"}
    )
    assert "error" in unknown_res
    assert unknown_res["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_mcp_tool_call_get_source_view() -> None:
    mock_source_view = AsyncMock()
    server = ReadOnlyMcpServer(source_view_service=mock_source_view)

    org_id = uuid4()
    repo_id = uuid4()
    snap_id = uuid4()
    principal_id = uuid4()
    file_id = uuid4()

    mock_result = SourceViewResultV1(
        file_id=file_id,
        file_path="src/main.py",
        file_sha256="abc123",
        representation=SourceRepresentation.MODEL_SAFE,
        requested_range=ByteRangeV1(start_byte=0, end_byte=100),
        actual_range=ByteRangeV1(start_byte=0, end_byte=100),
        content_bytes=b"def main(): pass",
        content_text="def main(): pass",
        content_sha256="def456",
        total_file_size=100,
    )
    mock_source_view.get.return_value = mock_result

    req = {
        "jsonrpc": "2.0",
        "id": 10,
        "method": "tools/call",
        "params": {
            "name": "get_source_view",
            "arguments": {
                "org_id": str(org_id),
                "repository_id": str(repo_id),
                "snapshot_id": str(snap_id),
                "principal_id": str(principal_id),
                "file_id": str(file_id),
                "start_byte": 0,
                "end_byte": 100,
            },
        },
    }

    res = await server.handle_request(req)
    assert res["id"] == 10
    assert res["result"]["isError"] is False
    content_text = res["result"]["content"][0]["text"]
    parsed = json.loads(content_text)
    assert parsed["file_path"] == "src/main.py"
    mock_source_view.get.assert_awaited_once()


@pytest.mark.asyncio
async def test_mcp_tool_call_search_exact() -> None:
    mock_lexical = AsyncMock()
    server = ReadOnlyMcpServer(lexical_service=mock_lexical)

    org_id = uuid4()
    repo_id = uuid4()
    snap_id = uuid4()
    principal_id = uuid4()

    scope = SearchScopeV1(
        org_id=org_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        principal_id=principal_id,
    )
    mock_lexical.search_exact.return_value = SearchResponseV1(
        scope=scope,
        mode=SearchMode.EXACT,
        query="authenticate",
        total_candidates=1,
        hits=(),
    )

    req = {
        "jsonrpc": "2.0",
        "id": 11,
        "method": "tools/call",
        "params": {
            "name": "search_exact",
            "arguments": {
                "org_id": str(org_id),
                "repository_id": str(repo_id),
                "snapshot_id": str(snap_id),
                "principal_id": str(principal_id),
                "query": "authenticate",
            },
        },
    }

    res = await server.handle_request(req)
    assert res["id"] == 11
    assert res["result"]["isError"] is False
    parsed = json.loads(res["result"]["content"][0]["text"])
    assert parsed["query"] == "authenticate"
    assert parsed["total_candidates"] == 1


@pytest.mark.asyncio
async def test_mcp_tool_call_hybrid_and_context_assembly() -> None:
    mock_hybrid = AsyncMock()
    mock_context = AsyncMock()
    server = ReadOnlyMcpServer(hybrid_service=mock_hybrid, context_service=mock_context)

    org_id = uuid4()
    repo_id = uuid4()
    snap_id = uuid4()
    principal_id = uuid4()
    scope = SearchScopeV1(
        org_id=org_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        principal_id=principal_id,
    )

    chunk_id = uuid4()
    file_id = uuid4()
    hit = HybridSearchHitV1(
        chunk_id=chunk_id,
        file_id=file_id,
        file_path="src/auth.py",
        chunk_kind=ChunkKind.FUNCTION,
        symbol_name="login",
        start_line=1,
        end_line=10,
        start_byte=0,
        end_byte=50,
        score=0.032,
        snippet="def login(): pass",
        content_sha256="abc12345",
        match_modalities=("exact", "dense"),
        modality_ranks={"exact": 1, "dense": 1},
    )
    mock_hybrid.search_hybrid.return_value = HybridSearchResponseV1(
        scope=scope,
        query="login",
        total_candidates=1,
        hits=(hit,),
    )

    item = ContextItemV1(
        chunk_id=chunk_id,
        file_id=file_id,
        file_path="src/auth.py",
        symbol_name="login",
        chunk_kind=ChunkKind.FUNCTION,
        start_line=1,
        end_line=10,
        start_byte=0,
        end_byte=50,
        token_count=10,
        byte_count=50,
        safe_content="def login(): pass",
        citation_key="[CIT:1]",
        relevance_score=0.032,
        match_modalities=("exact", "dense"),
    )
    mock_context.assemble_context.return_value = ContextPacketV1(
        packet_id=uuid4(),
        scope=scope,
        query="login",
        items=(item,),
        total_tokens=10,
        total_bytes=50,
        items_count=1,
        omitted_count=0,
        packet_sha256="abc12345",
        created_at=datetime.now(UTC),
    )

    req = {
        "jsonrpc": "2.0",
        "id": 12,
        "method": "tools/call",
        "params": {
            "name": "assemble_context",
            "arguments": {
                "org_id": str(org_id),
                "repository_id": str(repo_id),
                "snapshot_id": str(snap_id),
                "principal_id": str(principal_id),
                "query": "login",
                "max_tokens": 2048,
            },
        },
    }

    res = await server.handle_request(req)
    assert res["id"] == 12
    assert res["result"]["isError"] is False
    parsed = json.loads(res["result"]["content"][0]["text"])
    assert parsed["total_tokens"] == 10
    assert len(parsed["items"]) == 1


@pytest.mark.asyncio
async def test_mcp_tool_call_query_memory() -> None:
    mock_memory = AsyncMock()
    server = ReadOnlyMcpServer(memory_service=mock_memory)

    org_id = uuid4()
    repo_id = uuid4()
    principal_id = uuid4()

    empty_items: tuple[MemoryEntryV1, ...] = ()
    mock_memory.query_memory.return_value = MemoryQueryResponseV1(
        org_id=org_id,
        repository_id=repo_id,
        query="csrf",
        total_candidates=1,
        items=empty_items,
    )

    req = {
        "jsonrpc": "2.0",
        "id": 13,
        "method": "tools/call",
        "params": {
            "name": "query_memory",
            "arguments": {
                "org_id": str(org_id),
                "repository_id": str(repo_id),
                "principal_id": str(principal_id),
                "query": "csrf",
            },
        },
    }

    res = await server.handle_request(req)
    assert res["id"] == 13
    assert res["result"]["isError"] is False
    parsed = json.loads(res["result"]["content"][0]["text"])
    assert parsed["query"] == "csrf"


@pytest.mark.asyncio
async def test_mcp_security_scope_rejection_and_mutating_tool_forbiddance() -> None:
    mock_memory = AsyncMock()
    mock_memory.query_memory.side_effect = ScopeViolation("Principal not authorized for repo")
    server = ReadOnlyMcpServer(memory_service=mock_memory)

    # 1. ScopeViolation wrapped as isError: True
    req = {
        "jsonrpc": "2.0",
        "id": 14,
        "method": "tools/call",
        "params": {
            "name": "query_memory",
            "arguments": {
                "org_id": str(uuid4()),
                "repository_id": str(uuid4()),
                "principal_id": str(uuid4()),
                "query": "unauthorized",
            },
        },
    }
    res = await server.handle_request(req)
    assert res["result"]["isError"] is True
    assert "Security/Scope Violation" in res["result"]["content"][0]["text"]

    empty_args: dict[str, object] = {}
    for forbidden_tool in ("write_file", "delete_file", "run_bash", "modify_index"):
        bad_req = {
            "jsonrpc": "2.0",
            "id": 15,
            "method": "tools/call",
            "params": {"name": forbidden_tool, "arguments": empty_args},
        }
        bad_res = await server.handle_request(bad_req)
        assert bad_res["result"]["isError"] is True
        assert "Unknown or unauthorized tool" in bad_res["result"]["content"][0]["text"]

    # 3. Malformed UUID parameter
    param_req = {
        "jsonrpc": "2.0",
        "id": 16,
        "method": "tools/call",
        "params": {
            "name": "query_memory",
            "arguments": {
                "org_id": "not-a-valid-uuid",
                "repository_id": str(uuid4()),
                "principal_id": str(uuid4()),
                "query": "test",
            },
        },
    }
    param_res = await server.handle_request(param_req)
    assert param_res["result"]["isError"] is True
    assert "Invalid Parameters" in param_res["result"]["content"][0]["text"]
