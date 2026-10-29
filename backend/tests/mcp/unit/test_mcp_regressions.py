"""Regression coverage for connected MCP authority and runtime boundaries."""

import asyncio
import io
import json
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from app.domain.scope import OrgRole, TenantContext
from app.mcp.server import McpConcurrencyLimiter, ReadOnlyMcpServer, create_mcp_server


async def test_source_preview_without_cache_does_not_write():
    import hashlib

    from app.contracts.source_view import PrivacyPolicyV1, SourceViewRequestV1
    from app.services.source_view import SourceFileRow, SourceViewService

    session = AsyncMock()
    rows = MagicMock()
    rows.mappings.return_value.one_or_none.return_value = None
    session.execute.return_value = rows
    raw = b"print(1)\n"
    file = SourceFileRow(uuid4(), "a.py", hashlib.sha256(raw).hexdigest(), len(raw))
    request = SourceViewRequestV1(
        org_id=uuid4(),
        repository_id=uuid4(),
        snapshot_id=uuid4(),
        principal_id=uuid4(),
        file_id=file.id,
    )
    service = SourceViewService(session, MagicMock(), persist_metadata=False)
    result = await service._build_model_safe_view(raw, file, request, PrivacyPolicyV1())
    assert result.content_text == "print(1)\n"
    session.execute.assert_awaited_once()
    assert str(session.execute.call_args.args[0]).startswith("SELECT")


async def test_unknown_tool_name_is_not_copied_to_telemetry():
    import mcp.types as types
    from app.mcp.telemetry import McpTelemetry

    logs = io.StringIO()
    server = create_mcp_server(ReadOnlyMcpServer(), telemetry=McpTelemetry(logs))
    handler = server._request_handlers["tools/call"].handler
    await handler(MagicMock(), types.CallToolRequestParams(name="PRIVATE_NAME_CANARY"))
    assert "PRIVATE_NAME_CANARY" not in logs.getvalue()
    assert json.loads(logs.getvalue())["tool_name"] == "unknown"


async def test_cancelled_waiter_releases_queue_capacity():
    limiter = McpConcurrencyLimiter(max_active=1, max_waiting=1)
    entered = asyncio.Event()

    async def wait():
        entered.set()
        async with limiter.throttle():
            pass

    async with limiter.throttle():
        task = asyncio.create_task(wait())
        await entered.wait()
        assert limiter._waiting == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert limiter._waiting == 0


async def test_sdk_catalogue_exposes_output_schemas_and_read_annotations():
    server = create_mcp_server(ReadOnlyMcpServer())
    from mcp.types import PaginatedRequestParams

    handler = server._request_handlers["tools/list"].handler
    result = await handler(MagicMock(), PaginatedRequestParams())
    for tool in result.tools:
        assert tool.output_schema["type"] == "object"
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.open_world_hint is False


@pytest.mark.parametrize("tool", ["get_evidence", "graph_neighbors"])
async def test_snapshot_authority_precedes_data_queries(monkeypatch, tool):
    from app.domain.errors import ScopeViolation

    identity = TenantContext(org_id=uuid4(), principal_id=uuid4(), role=OrgRole.MEMBER)
    session = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    denied = AsyncMock(side_effect=ScopeViolation("snapshot unavailable"))
    monkeypatch.setattr("app.mcp.server.require_snapshot", denied, raising=False)
    args = {"repository_id": str(uuid4()), "snapshot_id": str(uuid4())}
    args["evidence_id" if tool == "get_evidence" else "node_id"] = str(uuid4())
    facade = ReadOnlyMcpServer(identity=identity, db_factory=factory)
    with pytest.raises(ScopeViolation, match="snapshot unavailable"):
        await facade.dispatch_tool(tool, args)
    denied.assert_awaited_once()
    session.execute.assert_not_awaited()


async def test_runtime_wires_isolated_sessions_and_no_embedding_provider(monkeypatch):
    from app.mcp.runtime import ConnectedMcpServer

    sessions = [AsyncMock(), AsyncMock()]
    sessions_seen = []
    factory = MagicMock()
    contexts = []
    for session in sessions:
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=session)
        context.__aexit__ = AsyncMock(return_value=False)
        transaction = MagicMock()
        transaction.__aenter__ = AsyncMock(return_value=None)
        transaction.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(return_value=transaction)
        contexts.append(context)
    factory.side_effect = contexts
    monkeypatch.setattr("app.mcp.runtime.require_snapshot", AsyncMock())

    async def inspect_services(self, name, args):
        assert self.lexical_service is not None
        assert self.hybrid_service.dense_service.embedding_port is None
        assert self.memory_service.evidence is not None
        assert self.context_service is not None
        sessions_seen.append(self.lexical_service.s)
        await asyncio.sleep(0)
        return '{"ok":true}'

    monkeypatch.setattr(ReadOnlyMcpServer, "_dispatch_tool", inspect_services)
    identity = TenantContext(org_id=uuid4(), principal_id=uuid4(), role=OrgRole.MEMBER)
    repo = uuid4()
    runtime = ConnectedMcpServer(
        identity=identity, repository_allowlist=frozenset({repo}), factory=factory
    )
    args = {"repository_id": str(repo), "snapshot_id": str(uuid4()), "query": "print"}
    assert await asyncio.gather(*[runtime._dispatch_tool("search_exact", args) for _ in range(2)])
    assert sessions_seen == sessions
    assert sessions_seen[0] is not sessions_seen[1]


async def test_graph_projection_uses_real_columns_and_omits_arbitrary_properties(monkeypatch):
    identity = TenantContext(org_id=uuid4(), principal_id=uuid4(), role=OrgRole.MEMBER)
    repo, snapshot, run, node, file = (uuid4() for _ in range(5))
    session = AsyncMock()
    root_result, edge_result = MagicMock(), MagicMock()
    root_result.mappings.return_value.one_or_none.return_value = {
        "local_id": node,
        "file_id": file,
        "node_kind": "function",
        "symbol_name": "demo",
        "start_line": 1,
        "attributes": {"secret": "PRIVATE_GRAPH_CANARY"},
    }
    edge_result.mappings.return_value.all.return_value = []
    session.execute.side_effect = [root_result, edge_result]
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("app.mcp.server.require_snapshot", AsyncMock())
    monkeypatch.setattr("app.repositories.analysis_repository.AnalysisRepository.get", AsyncMock())
    facade = ReadOnlyMcpServer(identity=identity, db_factory=factory)
    result = json.loads(
        await facade.dispatch_tool(
            "graph_neighbors",
            {
                "repository_id": str(repo),
                "snapshot_id": str(snapshot),
                "structural_run_id": str(run),
                "node_id": str(node),
            },
        )
    )
    assert result["nodes"][0]["id"] == str(node)
    assert result["nodes"][0]["file_id"] == str(file)
    assert result["coverage_status"] == "partial"
    assert "PRIVATE_GRAPH_CANARY" not in json.dumps(result)


async def test_response_limit_includes_escaped_text_and_structured_copy():
    import mcp.types as types
    from app.contracts.search import SearchResponseV1, SearchScopeV1

    identity = TenantContext(org_id=uuid4(), principal_id=uuid4(), role=OrgRole.MEMBER)
    repo, snapshot = uuid4(), uuid4()
    scope = SearchScopeV1(
        org_id=identity.org_id,
        principal_id=identity.principal_id,
        repository_id=repo,
        snapshot_id=snapshot,
    )
    lexical = AsyncMock()
    lexical.search_exact.return_value = SearchResponseV1(
        scope=scope,
        mode="exact",
        query="\x00" * 18000,
        total_candidates=0,
        hits=(),
    )
    server = create_mcp_server(ReadOnlyMcpServer(identity=identity, lexical_service=lexical))
    handler = server._request_handlers["tools/call"].handler
    result = await handler(
        MagicMock(),
        types.CallToolRequestParams(
            name="search_exact",
            arguments={"repository_id": str(repo), "snapshot_id": str(snapshot), "query": "print"},
        ),
    )
    assert result.is_error
    assert result.structured_content["code"] == "response_too_large"


async def test_sdk_cancellation_stops_handler_and_allows_next_call():
    import anyio
    from app.contracts.search import SearchResponseV1, SearchScopeV1
    from mcp.client.session import ClientSession
    from tests.mcp.unit.test_mcp_sdk_client import _create_stream_pair

    entered, stopped = asyncio.Event(), asyncio.Event()
    identity = TenantContext(org_id=uuid4(), principal_id=uuid4(), role=OrgRole.MEMBER)
    repo, snapshot = uuid4(), uuid4()
    lexical = AsyncMock()

    async def long_search(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    lexical.search_exact.side_effect = long_search
    limiter = McpConcurrencyLimiter()
    server = create_mcp_server(
        ReadOnlyMcpServer(identity=identity, lexical_service=lexical), limiter=limiter
    )
    client_send, server_receive, server_send, client_receive = _create_stream_pair()
    args = {"repository_id": str(repo), "snapshot_id": str(snapshot), "query": "print"}
    async with anyio.create_task_group() as group:
        group.start_soon(
            server.run, server_receive, server_send, server.create_initialization_options()
        )
        async with ClientSession(client_receive, client_send) as client:
            await client.initialize()
            pending = asyncio.create_task(client.call_tool("search_exact", args))
            await asyncio.wait_for(entered.wait(), 2)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            await asyncio.wait_for(stopped.wait(), 2)
            assert limiter._active == 0 and limiter._waiting == 0
            lexical.search_exact.side_effect = None
            lexical.search_exact.return_value = SearchResponseV1(
                scope=SearchScopeV1(
                    org_id=identity.org_id,
                    principal_id=identity.principal_id,
                    repository_id=repo,
                    snapshot_id=snapshot,
                ),
                mode="exact",
                query="print",
                total_candidates=0,
                hits=(),
            )
            result = await client.call_tool("search_exact", args)
            assert not result.is_error
        group.cancel_scope.cancel()
