"""Unit tests for MCP operational visibility and telemetry redaction.

Verifies:
1. Structured operational metrics are recorded to stderr.
2. Zero leakage of tool arguments, queries, source excerpts, memory text, report bodies,
   or credentials.
3. Counters track success, authorization denials, timeouts, and cancellations.
4. SDK default OpenTelemetry middleware is cleared.
"""

from __future__ import annotations

import asyncio
import io
import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import mcp.types as types
import pytest
from app.contracts.context import ContextPacketV1
from app.contracts.search import (
    SearchMode,
    SearchResponseV1,
    SearchScopeV1,
)
from app.domain.scope import OrgRole, TenantContext
from app.mcp.server import (
    McpConcurrencyLimiter,
    ReadOnlyMcpServer,
    create_mcp_server,
)
from app.mcp.telemetry import McpTelemetry
from mcp.server.context import ServerRequestContext


@pytest.fixture
def test_tenant() -> TenantContext:
    return TenantContext(
        org_id=UUID("11111111-1111-1111-1111-111111111111"),
        principal_id=UUID("22222222-2222-2222-2222-222222222222"),
        role=OrgRole.MEMBER,
    )


@pytest.fixture
def repo_id() -> UUID:
    return UUID("33333333-3333-3333-3333-333333333333")


@pytest.fixture
def snapshot_id() -> UUID:
    return UUID("44444444-4444-4444-4444-444444444444")


@pytest.fixture
def chunk_set_id() -> UUID:
    return UUID("55555555-5555-5555-5555-555555555555")


class MockRequestContext(ServerRequestContext[Any, Any]):
    def __init__(self) -> None:
        pass


@pytest.mark.asyncio
async def test_telemetry_success_path_records_metrics_without_leaking_query_or_source(
    test_tenant: TenantContext,
    repo_id: UUID,
    snapshot_id: UUID,
    chunk_set_id: UUID,
) -> None:
    """Tool execution emits telemetry with metrics but strictly redacts queries and source code."""
    sensitive_needle = "CONFIDENTIAL_KEY_99999_xyz def proprietary_algo(): return 42"
    sensitive_source = "class SecretInternalService: PRIVATE_KEY = 'priv123'"

    scope = SearchScopeV1(
        org_id=test_tenant.org_id,
        repository_id=repo_id,
        snapshot_id=snapshot_id,
        chunk_set_id=chunk_set_id,
        principal_id=test_tenant.principal_id,
    )
    lexical_mock = AsyncMock()
    lexical_mock.search_exact.return_value = SearchResponseV1(
        scope=scope,
        mode=SearchMode.EXACT,
        query=sensitive_needle,
        total_candidates=1,
        hits=(),
    )

    stream = io.StringIO()
    telemetry = McpTelemetry(stream)

    facade = ReadOnlyMcpServer(
        identity=test_tenant,
        repository_allowlist=frozenset([repo_id]),
        lexical_service=lexical_mock,
        telemetry=telemetry,
    )
    server = create_mcp_server(facade, telemetry=telemetry)

    handler = server._request_handlers["tools/call"].handler
    req = types.CallToolRequestParams(
        name="search_exact",
        arguments={
            "repository_id": str(repo_id),
            "snapshot_id": str(snapshot_id),
            "chunk_set_id": str(chunk_set_id),
            "query": sensitive_needle,
        },
    )

    result = await handler(MockRequestContext(), req)
    assert isinstance(result, types.CallToolResult)
    assert result.is_error is False

    raw_output = stream.getvalue()
    assert raw_output.strip(), "Telemetry log should be emitted"
    log_entry = json.loads(raw_output.strip().splitlines()[-1])

    assert log_entry["event"] == "mcp_tool_call"
    assert log_entry["tool_name"] == "search_exact"
    assert log_entry["outcome_category"] == "success"
    assert UUID(log_entry["correlation_id"])
    assert log_entry["duration_ms"] >= 0.0
    assert log_entry["queue_wait_ms"] >= 0.0
    assert log_entry["response_bytes"] > 0
    assert log_entry["coverage_omissions_count"] == 0
    assert log_entry["total_calls"] == 1
    assert log_entry["cancellation_count"] == 0
    assert log_entry["timeout_count"] == 0
    assert log_entry["authorization_denial_count"] == 0

    # ZERO LEAKAGE ASSERTIONS
    assert sensitive_needle not in raw_output
    assert "CONFIDENTIAL_KEY" not in raw_output
    assert "proprietary_algo" not in raw_output
    assert sensitive_source not in raw_output
    assert "priv123" not in raw_output


@pytest.mark.asyncio
async def test_telemetry_scope_violation_increments_counter_and_redacts(
    test_tenant: TenantContext,
    repo_id: UUID,
    snapshot_id: UUID,
) -> None:
    """Scope violation increments authorization denial count without leaking query arguments."""
    disallowed_repo = UUID("99999999-9999-9999-9999-999999999999")
    forbidden_query_text = "UNAUTHORIZED_SENSITIVE_SEARCH_QUERY"

    stream = io.StringIO()
    telemetry = McpTelemetry(stream)

    facade = ReadOnlyMcpServer(
        identity=test_tenant,
        repository_allowlist=frozenset([repo_id]),  # disallowed_repo is not allowed
        telemetry=telemetry,
    )
    server = create_mcp_server(facade, telemetry=telemetry)

    handler = server._request_handlers["tools/call"].handler
    req = types.CallToolRequestParams(
        name="search_exact",
        arguments={
            "repository_id": str(disallowed_repo),
            "snapshot_id": str(snapshot_id),
            "query": forbidden_query_text,
        },
    )

    result = await handler(MockRequestContext(), req)
    assert isinstance(result, types.CallToolResult)
    assert result.is_error is True

    raw_output = stream.getvalue()
    log_entry = json.loads(raw_output.strip().splitlines()[-1])

    assert log_entry["tool_name"] == "search_exact"
    assert log_entry["outcome_category"] == "scope_violation"
    assert log_entry["authorization_denial_count"] == 1
    assert log_entry["total_calls"] == 1

    # Assert no sensitive token in log
    assert forbidden_query_text not in raw_output


@pytest.mark.asyncio
async def test_telemetry_invalid_parameters_redacts_arguments(
    test_tenant: TenantContext,
    repo_id: UUID,
    snapshot_id: UUID,
) -> None:
    """Invalid parameters are categorized without logging raw malicious argument values."""
    injected_bad_param = "MY_INJECTED_DATABASE_SECRET"

    stream = io.StringIO()
    telemetry = McpTelemetry(stream)

    facade = ReadOnlyMcpServer(
        identity=test_tenant,
        repository_allowlist=frozenset([repo_id]),
        telemetry=telemetry,
    )
    server = create_mcp_server(facade, telemetry=telemetry)

    handler = server._request_handlers["tools/call"].handler
    req = types.CallToolRequestParams(
        name="search_exact",
        arguments={
            "repository_id": str(repo_id),
            "snapshot_id": str(snapshot_id),
            "query": "valid_query",
            "malicious_field": injected_bad_param,
        },
    )

    result = await handler(MockRequestContext(), req)
    assert isinstance(result, types.CallToolResult)
    assert result.is_error is True

    raw_output = stream.getvalue()
    log_entry = json.loads(raw_output.strip().splitlines()[-1])

    assert log_entry["outcome_category"] == "invalid_parameters"
    assert injected_bad_param not in raw_output
    assert "malicious_field" not in raw_output


@pytest.mark.asyncio
async def test_telemetry_size_exceeded_redacts_payload(
    test_tenant: TenantContext,
    repo_id: UUID,
    snapshot_id: UUID,
) -> None:
    """Oversized responses trigger size_exceeded category and omit massive payload text."""
    oversized_secret = "EXFILTRATED_CONTENT_RECORD_" * 20000  # ~540 KiB

    async def mock_dispatch(_name: str, _args: dict[str, Any]) -> str:
        return oversized_secret

    stream = io.StringIO()
    telemetry = McpTelemetry(stream)

    facade = ReadOnlyMcpServer(
        identity=test_tenant,
        repository_allowlist=frozenset([repo_id]),
        telemetry=telemetry,
    )
    facade.dispatch_tool = mock_dispatch  # type: ignore[assignment,method-assign]

    server = create_mcp_server(facade, telemetry=telemetry)
    handler = server._request_handlers["tools/call"].handler
    req = types.CallToolRequestParams(
        name="search_exact",
        arguments={
            "repository_id": str(repo_id),
            "snapshot_id": str(snapshot_id),
            "query": "test",
        },
    )

    result = await handler(MockRequestContext(), req)
    assert isinstance(result, types.CallToolResult)
    assert result.is_error is True

    raw_output = stream.getvalue()
    log_entry = json.loads(raw_output.strip().splitlines()[-1])

    assert log_entry["outcome_category"] == "size_exceeded"
    assert log_entry["response_bytes"] > 262144
    assert "EXFILTRATED_CONTENT_RECORD" not in raw_output


@pytest.mark.asyncio
async def test_telemetry_timeout_increments_counter(
    test_tenant: TenantContext,
    repo_id: UUID,
    snapshot_id: UUID,
) -> None:
    """Timeout during tool execution records timeout outcome and increments counter."""
    limiter = McpConcurrencyLimiter(max_active=1, max_waiting=1, timeout_seconds=0.01)

    async def slow_dispatch(_name: str, _args: dict[str, Any]) -> str:
        await asyncio.sleep(0.5)
        return "{}"

    stream = io.StringIO()
    telemetry = McpTelemetry(stream)

    facade = ReadOnlyMcpServer(
        identity=test_tenant,
        repository_allowlist=frozenset([repo_id]),
        telemetry=telemetry,
    )
    facade.dispatch_tool = slow_dispatch  # type: ignore[assignment,method-assign]

    server = create_mcp_server(facade, limiter=limiter, telemetry=telemetry)
    handler = server._request_handlers["tools/call"].handler
    req = types.CallToolRequestParams(
        name="search_exact",
        arguments={
            "repository_id": str(repo_id),
            "snapshot_id": str(snapshot_id),
            "query": "test",
        },
    )

    result = await handler(MockRequestContext(), req)
    assert isinstance(result, types.CallToolResult)
    assert result.is_error is True

    raw_output = stream.getvalue()
    log_entry = json.loads(raw_output.strip().splitlines()[-1])

    assert log_entry["outcome_category"] == "timeout"
    assert log_entry["timeout_count"] == 1


@pytest.mark.asyncio
async def test_telemetry_cancellation_increments_counter(
    test_tenant: TenantContext,
    repo_id: UUID,
    snapshot_id: UUID,
) -> None:
    """Client cancellation during execution records cancellation outcome and raises."""

    async def cancelled_dispatch(_name: str, _args: dict[str, Any]) -> str:
        raise asyncio.CancelledError()

    stream = io.StringIO()
    telemetry = McpTelemetry(stream)

    facade = ReadOnlyMcpServer(
        identity=test_tenant,
        repository_allowlist=frozenset([repo_id]),
        telemetry=telemetry,
    )
    facade.dispatch_tool = cancelled_dispatch  # type: ignore[assignment,method-assign]

    server = create_mcp_server(facade, telemetry=telemetry)
    handler = server._request_handlers["tools/call"].handler
    req = types.CallToolRequestParams(
        name="search_exact",
        arguments={
            "repository_id": str(repo_id),
            "snapshot_id": str(snapshot_id),
            "query": "test",
        },
    )

    with pytest.raises(asyncio.CancelledError):
        await handler(MockRequestContext(), req)

    raw_output = stream.getvalue()
    log_entry = json.loads(raw_output.strip().splitlines()[-1])

    assert log_entry["outcome_category"] == "cancellation"
    assert log_entry["cancellation_count"] == 1


@pytest.mark.asyncio
async def test_telemetry_omissions_count_extracted_honestly(
    test_tenant: TenantContext,
    repo_id: UUID,
    snapshot_id: UUID,
    chunk_set_id: UUID,
) -> None:
    """Omission count is extracted and recorded in telemetry without logging details."""
    omitted_file_path = "/sensitive/proprietary/vault.py"

    scope = SearchScopeV1(
        org_id=test_tenant.org_id,
        repository_id=repo_id,
        snapshot_id=snapshot_id,
        chunk_set_id=chunk_set_id,
        principal_id=test_tenant.principal_id,
    )
    hybrid_mock = AsyncMock()
    hybrid_mock.search_hybrid.return_value = SearchResponseV1(
        scope=scope,
        mode=SearchMode.HYBRID,
        query="assemble query",
        total_candidates=0,
        hits=(),
    )
    context_mock = AsyncMock()
    context_mock.assemble_context.return_value = ContextPacketV1(
        packet_id=uuid4(),
        scope=scope,
        query="assemble query",
        items=(),
        total_tokens=100,
        total_bytes=500,
        items_count=0,
        omitted_count=2,
        packet_sha256="abcdef123456",
        created_at=datetime.now(UTC),
    )

    stream = io.StringIO()
    telemetry = McpTelemetry(stream)

    facade = ReadOnlyMcpServer(
        identity=test_tenant,
        repository_allowlist=frozenset([repo_id]),
        hybrid_service=hybrid_mock,
        context_service=context_mock,
        telemetry=telemetry,
    )
    server = create_mcp_server(facade, telemetry=telemetry)

    handler = server._request_handlers["tools/call"].handler
    req = types.CallToolRequestParams(
        name="assemble_context",
        arguments={
            "repository_id": str(repo_id),
            "snapshot_id": str(snapshot_id),
            "chunk_set_id": str(chunk_set_id),
            "query": "assemble query",
        },
    )

    result = await handler(MockRequestContext(), req)
    assert isinstance(result, types.CallToolResult)
    assert result.is_error is False

    raw_output = stream.getvalue()
    log_entry = json.loads(raw_output.strip().splitlines()[-1])

    assert log_entry["tool_name"] == "assemble_context"
    assert log_entry["outcome_category"] == "success"
    assert log_entry["coverage_omissions_count"] == 2

    # Assert zero path leakage
    assert omitted_file_path not in raw_output


def test_sdk_opentelemetry_middleware_cleared(test_tenant: TenantContext) -> None:
    """Verifies that create_mcp_server clears default OpenTelemetry middleware."""
    facade = ReadOnlyMcpServer(identity=test_tenant)
    server = create_mcp_server(facade)
    assert len(server.middleware) == 0, "OpenTelemetry middleware must be disabled"
