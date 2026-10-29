"""Unit tests for the completed 13-tool MCP read-only catalogue.

Verifies all 13 tools via typed catalogue dispatch:
- list_findings adapter and pagination
- list_claims projection from investigation results
- trace_feature rooted at a finding
- search_hybrid dense query prohibition and lexical fallback labeling
- query_memory retrieval with version and history classification
- get_index_status adapter
- get_source_view adapter
- search_exact adapter
- assemble_context adapter
- get_investigation_status adapter
- get_investigation_result adapter
- get_evidence adapter
- graph_neighbors adapter
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from app.contracts.chunking import ChunkKind
from app.contracts.context import ContextPacketV1
from app.contracts.mcp import (
    GraphNeighborsResultV1,
    ListClaimsResultV1,
    ListFindingsResultV1,
    McpEvidenceResultV1,
    McpInvestigationResultV1,
    McpInvestigationStatusV1,
)
from app.contracts.memory import (
    MemoryEntryV1,
    MemoryKind,
    MemoryQueryResponseV1,
    MemoryState,
)
from app.contracts.search import (
    HybridSearchHitV1,
    HybridSearchResponseV1,
    SearchMode,
    SearchResponseV1,
    SearchScopeV1,
)
from app.contracts.source_view import ByteRangeV1, SourceRepresentation, SourceViewResultV1
from app.contracts.trace import FeatureTraceV1, TraceNodeV1
from app.domain.scope import OrgRole, TenantContext
from app.mcp.server import ReadOnlyMcpServer
from app.services.findings_query import FindingListResponseV1, FindingSummaryV1


@pytest.fixture
def mock_tenant() -> TenantContext:
    return TenantContext(
        org_id=uuid4(),
        principal_id=uuid4(),
        role=OrgRole.MEMBER,
    )


@pytest.mark.asyncio
async def test_list_findings_adapter(mock_tenant: TenantContext) -> None:
    repo_id = uuid4()
    snap_id = uuid4()
    run_id = uuid4()
    finding_id = uuid4()

    mock_findings = AsyncMock()
    mock_findings.list_findings.return_value = FindingListResponseV1(
        items=[
            FindingSummaryV1(
                id=finding_id,
                run_id=run_id,
                category="source_pattern",
                rule_id="RULE-001",
                severity="high",
                title="Insecure deserialization",
                file_path="src/service.py",
                line_number=42,
                disposition="confirmed",
                support_reason="current_support",
                triage_state="confirmed",
                triage_reason="none",
                triage_version=1,
            )
        ],
        next_cursor=None,
        total_count=1,
    )

    server = ReadOnlyMcpServer(
        identity=mock_tenant,
        findings_service=mock_findings,
    )

    res_raw = await server.dispatch_tool(
        "list_findings",
        {
            "repository_id": str(repo_id),
            "snapshot_id": str(snap_id),
            "run_id": str(run_id),
            "category": "source_pattern",
            "severity": "high",
        },
    )

    data = json.loads(res_raw)
    result = ListFindingsResultV1.model_validate(data)
    assert result.repository_id == repo_id
    assert result.snapshot_id == snap_id
    assert result.run_id == run_id
    assert len(result.findings) == 1
    assert result.findings[0].id == finding_id
    assert result.findings[0].rule_id == "RULE-001"
    assert result.findings[0].severity == "high"
    assert result.findings[0].current_support == "current_support"
    assert result.findings[0].line == 42


@pytest.mark.asyncio
async def test_list_claims_adapter(mock_tenant: TenantContext) -> None:
    repo_id = uuid4()
    inv_id = uuid4()
    claim_id = uuid4()
    evidence_id = uuid4()

    mock_inv_query = AsyncMock()
    mock_detail = AsyncMock()
    mock_detail.summary.snapshot_id = uuid4()
    mock_detail.current_support_available = True
    mock_detail.report = {
        "claims": [
            {
                "claim_id": str(claim_id),
                "statement": "Command injection vulnerability exists in parse_args",
                "kind": "vulnerability",
                "evidence_ids": [str(evidence_id)],
            }
        ],
        "verifications": [
            {
                "claim_id": str(claim_id),
                "status": "verified",
                "resolved_file": "cli/parser.py",
                "resolved_line_start": 10,
                "resolved_line_end": 25,
                "notes": "Verified by static call analysis",
            }
        ],
    }
    mock_inv_query.detail.return_value = mock_detail

    server = ReadOnlyMcpServer(
        identity=mock_tenant,
        investigation_query_service=mock_inv_query,
    )

    res_raw = await server.dispatch_tool(
        "list_claims",
        {
            "repository_id": str(repo_id),
            "investigation_id": str(inv_id),
        },
    )

    data = json.loads(res_raw)
    result = ListClaimsResultV1.model_validate(data)
    assert result.repository_id == repo_id
    assert result.investigation_id == inv_id
    assert result.total_count == 1
    assert len(result.claims) == 1
    assert result.claims[0].claim_id == claim_id
    assert result.claims[0].status == "verified"
    assert result.claims[0].evidence_refs == (evidence_id,)
    assert result.claims[0].resolved_file == "cli/parser.py"


@pytest.mark.asyncio
async def test_trace_feature_adapter(mock_tenant: TenantContext) -> None:
    repo_id = uuid4()
    snap_id = uuid4()
    finding_id = uuid4()
    node_id = uuid4()

    mock_trace_service = AsyncMock()
    mock_trace_service.trace.return_value = FeatureTraceV1(
        finding_id=finding_id,
        nodes=[
            TraceNodeV1(
                id=node_id,
                kind="function",
                label="auth_handler",
                file_path="auth.py",
                line=12,
            ),
        ],
        edges=[],
        hop_count=1,
    )

    server = ReadOnlyMcpServer(
        identity=mock_tenant,
        feature_trace_service=mock_trace_service,
    )

    res_raw = await server.dispatch_tool(
        "trace_feature",
        {
            "repository_id": str(repo_id),
            "snapshot_id": str(snap_id),
            "finding_id": str(finding_id),
            "max_hops": 1,
            "max_nodes": 20,
        },
    )

    data = json.loads(res_raw)
    assert UUID(data["finding_id"]) == finding_id
    assert len(data["nodes"]) == 1
    assert data["nodes"][0]["label"] == "auth_handler"


@pytest.mark.asyncio
async def test_search_hybrid_dense_prohibition(mock_tenant: TenantContext) -> None:
    repo_id = uuid4()
    snap_id = uuid4()
    chunk_id = uuid4()
    file_id = uuid4()

    mock_hybrid = AsyncMock()
    mock_hybrid.dense_search = None  # Explicitly disabled in MCP increment
    scope = SearchScopeV1(
        org_id=mock_tenant.org_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        principal_id=mock_tenant.principal_id,
    )
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
        match_modalities=("exact",),
        modality_ranks={"exact": 1},
    )
    mock_hybrid.search_hybrid.return_value = HybridSearchResponseV1(
        scope=scope,
        query="login authentication",
        total_candidates=1,
        hits=(hit,),
    )

    server = ReadOnlyMcpServer(
        identity=mock_tenant,
        hybrid_service=mock_hybrid,
    )

    res_raw = await server.dispatch_tool(
        "search_hybrid",
        {
            "repository_id": str(repo_id),
            "snapshot_id": str(snap_id),
            "query": "login authentication",
            "limit": 5,
        },
    )

    data = json.loads(res_raw)
    assert data["dense_coverage"] == "unavailable"
    assert data["fallback_mode"] == "lexical_only"
    assert len(data["hits"]) == 1
    assert data["hits"][0]["file_path"] == "src/auth.py"


@pytest.mark.asyncio
async def test_query_memory_adapter(mock_tenant: TenantContext) -> None:
    repo_id = uuid4()
    snap_id = uuid4()
    mem_id = uuid4()
    now = datetime.now(UTC)

    mock_memory = AsyncMock()
    mock_entry = MemoryEntryV1(
        memory_id=mem_id,
        org_id=mock_tenant.org_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        memory_kind=MemoryKind.ARCHITECTURE_FACT,
        topic="security",
        summary="Verified CSRF protection requirement on all state-changing endpoints.",
        created_at=now,
        updated_at=now,
        state=MemoryState.ACCEPTED,
        version=2,
    )
    mock_memory.query_memory.return_value = MemoryQueryResponseV1(
        org_id=mock_tenant.org_id,
        repository_id=repo_id,
        query="CSRF protection",
        total_candidates=1,
        items=(mock_entry,),
    )

    server = ReadOnlyMcpServer(
        identity=mock_tenant,
        memory_service=mock_memory,
    )

    res_raw = await server.dispatch_tool(
        "query_memory",
        {
            "repository_id": str(repo_id),
            "snapshot_id": str(snap_id),
            "query": "CSRF protection",
            "topic": "security",
            "history_mode": "current_only",
            "limit": 5,
        },
    )

    data = json.loads(res_raw)
    assert data["total_candidates"] == 1
    assert len(data["items"]) == 1
    assert UUID(data["items"][0]["memory_id"]) == mem_id
    assert data["items"][0]["version"] == 2
    assert "CSRF" in data["items"][0]["summary"]


@pytest.mark.asyncio
async def test_get_index_status_adapter(mock_tenant: TenantContext) -> None:
    repo_id = uuid4()
    snap_id = uuid4()
    job_id = uuid4()

    mock_index = AsyncMock()
    payload = {
        "job": {
            "id": str(job_id),
            "org_id": str(mock_tenant.org_id),
            "repository_id": str(repo_id),
            "snapshot_id": str(snap_id),
            "state": "succeeded",
            "index_spec_id": str(uuid4()),
            "result_index_id": str(uuid4()),
        },
        "receipt": None,
        "can_cancel": False,
        "can_retry": False,
    }
    mock_index.get.return_value = SimpleNamespace(model_dump=lambda **kwargs: payload)

    server = ReadOnlyMcpServer(
        identity=mock_tenant,
        index_service=mock_index,
    )

    res_raw = await server.dispatch_tool(
        "get_index_status",
        {
            "repository_id": str(repo_id),
            "snapshot_id": str(snap_id),
            "job_id": str(job_id),
        },
    )

    data = json.loads(res_raw)
    assert data["job"]["id"] == str(job_id)
    assert data["job"]["state"] == "succeeded"
    mock_index.get.assert_awaited_once_with(mock_tenant, repo_id, snap_id, job_id)


@pytest.mark.asyncio
async def test_get_source_view_adapter(mock_tenant: TenantContext) -> None:
    repo_id = uuid4()
    snap_id = uuid4()
    file_id = uuid4()

    mock_source_view = AsyncMock()
    mock_result = SourceViewResultV1(
        file_id=file_id,
        file_path="src/main.py",
        file_sha256="a" * 64,
        representation=SourceRepresentation.MODEL_SAFE,
        requested_range=ByteRangeV1(start_byte=0, end_byte=100),
        actual_range=ByteRangeV1(start_byte=0, end_byte=100),
        content_bytes=b"def main(): pass\n",
        content_text="def main(): pass\n",
        content_sha256="b" * 64,
        total_file_size=100,
    )
    mock_source_view.get.return_value = mock_result

    server = ReadOnlyMcpServer(
        identity=mock_tenant,
        source_view_service=mock_source_view,
    )

    res_raw = await server.dispatch_tool(
        "get_source_view",
        {
            "repository_id": str(repo_id),
            "snapshot_id": str(snap_id),
            "file_id": str(file_id),
            "start_byte": 0,
            "end_byte": 100,
        },
    )

    data = json.loads(res_raw)
    result = SourceViewResultV1.model_validate(data)
    assert result.file_id == file_id
    assert result.file_path == "src/main.py"
    assert result.content_text == "def main(): pass\n"
    mock_source_view.get.assert_awaited_once()


@pytest.mark.asyncio
async def test_search_exact_adapter(mock_tenant: TenantContext) -> None:
    repo_id = uuid4()
    snap_id = uuid4()
    chunk_set_id = uuid4()

    mock_lexical = AsyncMock()
    scope = SearchScopeV1(
        org_id=mock_tenant.org_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        chunk_set_id=chunk_set_id,
        principal_id=mock_tenant.principal_id,
    )
    mock_lexical.search_exact.return_value = SearchResponseV1(
        scope=scope,
        mode=SearchMode.EXACT,
        query="authenticate",
        total_candidates=1,
        hits=(),
    )

    server = ReadOnlyMcpServer(
        identity=mock_tenant,
        lexical_service=mock_lexical,
    )

    res_raw = await server.dispatch_tool(
        "search_exact",
        {
            "repository_id": str(repo_id),
            "snapshot_id": str(snap_id),
            "query": "authenticate",
            "chunk_set_id": str(chunk_set_id),
        },
    )

    data = json.loads(res_raw)
    result = SearchResponseV1.model_validate(data)
    assert result.query == "authenticate"
    assert result.scope.chunk_set_id == chunk_set_id
    mock_lexical.search_exact.assert_awaited_once()


@pytest.mark.asyncio
async def test_assemble_context_adapter(mock_tenant: TenantContext) -> None:
    repo_id = uuid4()
    snap_id = uuid4()
    chunk_set_id = uuid4()

    scope = SearchScopeV1(
        org_id=mock_tenant.org_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        chunk_set_id=chunk_set_id,
        principal_id=mock_tenant.principal_id,
    )

    mock_hybrid = AsyncMock()
    mock_hybrid.search_hybrid.return_value = HybridSearchResponseV1(
        scope=scope,
        query="database pool",
        total_candidates=0,
        hits=(),
    )

    now = datetime.now(UTC)
    mock_context = AsyncMock()
    mock_context.assemble_context.return_value = ContextPacketV1(
        packet_id=uuid4(),
        scope=scope,
        query="database pool",
        chunk_set_id=chunk_set_id,
        items=(),
        total_tokens=50,
        total_bytes=200,
        items_count=0,
        omitted_count=0,
        packet_sha256="c" * 64,
        created_at=now,
    )

    server = ReadOnlyMcpServer(
        identity=mock_tenant,
        hybrid_service=mock_hybrid,
        context_service=mock_context,
    )

    res_raw = await server.dispatch_tool(
        "assemble_context",
        {
            "repository_id": str(repo_id),
            "snapshot_id": str(snap_id),
            "query": "database pool",
            "chunk_set_id": str(chunk_set_id),
            "max_tokens": 4096,
        },
    )

    data = json.loads(res_raw)
    result = ContextPacketV1.model_validate(data)
    assert result.chunk_set_id == chunk_set_id
    assert result.query == "database pool"
    mock_context.assemble_context.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_investigation_status_adapter(mock_tenant: TenantContext) -> None:
    repo_id = uuid4()
    inv_id = uuid4()
    snap_id = uuid4()

    mock_inv_query = AsyncMock()
    summary = SimpleNamespace(
        id=inv_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        status="running",
        target_type="repository",
        query="Check session validation",
        current_step=2,
        max_steps=5,
        budget_limit_micros=1000000,
        spent_micros=200000,
        held_micros=100000,
        verdict=None,
        verdict_confidence=None,
        summary=None,
        failure_reason=None,
    )
    mock_detail = SimpleNamespace(
        summary=summary,
        job=None,
        current_support_available=True,
        available_actions=("cancel",),
        claims_count=1,
        evidence_count=2,
        events_count=3,
    )
    mock_inv_query.detail.return_value = mock_detail

    server = ReadOnlyMcpServer(
        identity=mock_tenant,
        investigation_query_service=mock_inv_query,
    )

    res_raw = await server.dispatch_tool(
        "get_investigation_status",
        {
            "repository_id": str(repo_id),
            "investigation_id": str(inv_id),
        },
    )

    data = json.loads(res_raw)
    result = McpInvestigationStatusV1.model_validate(data)
    assert result.investigation_id == inv_id
    assert result.repository_id == repo_id
    assert result.status == "running"
    assert result.claims_count == 1
    assert result.current_support_available is True


@pytest.mark.asyncio
async def test_get_investigation_result_adapter(mock_tenant: TenantContext) -> None:
    repo_id = uuid4()
    inv_id = uuid4()
    snap_id = uuid4()

    mock_inv_query = AsyncMock()
    summary = SimpleNamespace(
        id=inv_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        status="completed",
        verdict="verified",
        verdict_confidence=0.95,
        summary="Security boundary confirmed",
    )
    mock_detail = SimpleNamespace(
        summary=summary,
        current_support_available=True,
        claims_count=2,
        evidence_count=3,
        report={"title": "Security Review Report"},
    )
    mock_inv_query.detail.return_value = mock_detail

    server = ReadOnlyMcpServer(
        identity=mock_tenant,
        investigation_query_service=mock_inv_query,
    )

    res_raw = await server.dispatch_tool(
        "get_investigation_result",
        {
            "repository_id": str(repo_id),
            "investigation_id": str(inv_id),
        },
    )

    data = json.loads(res_raw)
    result = McpInvestigationResultV1.model_validate(data)
    assert result.investigation_id == inv_id
    assert result.status == "completed"
    assert result.verdict == "verified"
    assert result.report == {"title": "Security Review Report"}


@pytest.mark.asyncio
async def test_get_evidence_adapter(
    mock_tenant: TenantContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_id = uuid4()
    snap_id = uuid4()
    ev_id = uuid4()
    run_id = uuid4()
    file_id = uuid4()
    now = datetime.now(UTC)

    session = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    monkeypatch.setattr("app.mcp.server.require_snapshot", AsyncMock(), raising=False)

    mock_analysis_repo = MagicMock()
    mock_analysis_repo.get = AsyncMock()
    monkeypatch.setattr(
        "app.repositories.analysis_repository.AnalysisRepository",
        lambda s: mock_analysis_repo,
    )

    row_data = {
        "id": ev_id,
        "run_id": run_id,
        "provider_name": "semgrep",
        "provider_version": "1.0.0",
        "operation": "rule_scan",
        "digest": "d" * 64,
        "file_id": file_id,
        "file_path": "src/app.py",
        "start_line": 10,
        "start_col": 1,
        "end_line": 12,
        "end_col": 20,
        "observation_mode": "static_pattern",
        "basis": "ast_match",
        "coverage_status": "covered",
        "limitations": ["taint_untracked"],
        "captured_at": now,
    }
    mock_mappings = MagicMock()
    mock_mappings.mappings.return_value.one_or_none.return_value = row_data
    session.execute.return_value = mock_mappings

    server = ReadOnlyMcpServer(identity=mock_tenant, db_factory=factory)
    res_raw = await server.dispatch_tool(
        "get_evidence",
        {
            "repository_id": str(repo_id),
            "snapshot_id": str(snap_id),
            "evidence_id": str(ev_id),
        },
    )

    data = json.loads(res_raw)
    result = McpEvidenceResultV1.model_validate(data)
    assert result.id == ev_id
    assert result.provider_name == "semgrep"
    assert result.repository_id == repo_id
    assert result.snapshot_id == snap_id
    assert "raw_payload_omitted" in result.limitations
    assert result.file_path == "src/app.py"


@pytest.mark.asyncio
async def test_graph_neighbors_adapter(
    mock_tenant: TenantContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_id = uuid4()
    snap_id = uuid4()
    node_id = uuid4()
    structural_run_id = uuid4()
    file_id = uuid4()

    session = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    monkeypatch.setattr("app.mcp.server.require_snapshot", AsyncMock(), raising=False)

    mock_analysis_repo = MagicMock()
    mock_analysis_repo.get = AsyncMock()
    monkeypatch.setattr(
        "app.repositories.analysis_repository.AnalysisRepository",
        lambda s: mock_analysis_repo,
    )

    root_row = {
        "local_id": node_id,
        "file_id": file_id,
        "node_kind": "function",
        "symbol_name": "process_tokens",
        "file_path": "src/parser.py",
        "start_line": 20,
    }

    mock_root_res = MagicMock()
    mock_root_res.mappings.return_value.one_or_none.return_value = root_row

    mock_edges_res = MagicMock()
    mock_edges_res.mappings.return_value.all.return_value = []

    session.execute.side_effect = [mock_root_res, mock_edges_res]

    server = ReadOnlyMcpServer(identity=mock_tenant, db_factory=factory)
    res_raw = await server.dispatch_tool(
        "graph_neighbors",
        {
            "repository_id": str(repo_id),
            "snapshot_id": str(snap_id),
            "node_id": str(node_id),
            "structural_run_id": str(structural_run_id),
            "max_hops": 1,
        },
    )

    data = json.loads(res_raw)
    result = GraphNeighborsResultV1.model_validate(data)
    assert result.repository_id == repo_id
    assert result.root_node_id == node_id
    assert result.structural_run_id == structural_run_id
    assert len(result.nodes) == 1
    assert result.nodes[0].label == "process_tokens"
    assert len(result.edges) == 0
