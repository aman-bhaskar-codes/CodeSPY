"""Unit tests for MCP investigation status and result tools."""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from app.contracts.investigation_jobs import (
    InvestigationDetailResponseV2,
    InvestigationJobRecordV1,
    InvestigationSummaryV2,
)
from app.domain.errors import ScopeViolation
from app.domain.scope import OrgRole, TenantContext
from app.mcp.server import ReadOnlyMcpServer


def _make_sample_detail(
    org_id=None,
    repo_id=None,
    inv_id=None,
    status="running",
    verdict=None,
    report=None,
) -> InvestigationDetailResponseV2:
    org_id = org_id or uuid4()
    repo_id = repo_id or uuid4()
    inv_id = inv_id or uuid4()
    now = datetime.now(UTC)

    summary = InvestigationSummaryV2(
        id=inv_id,
        org_id=org_id,
        repository_id=repo_id,
        snapshot_id=uuid4(),
        finding_id=None,
        target_type="repository",
        query="Investigate auth bypass",
        status=status,
        mode="deep",
        depth="exhaustive",
        current_step=3,
        max_steps=10,
        budget_limit_micros=5000000,
        spent_micros=1200000,
        held_micros=500000,
        verdict=verdict,
        verdict_confidence=0.92 if verdict else None,
        summary="Found potential issue" if verdict else None,
        failure_reason=None,
        spec_hash="a" * 64,
        current_job_id=uuid4(),
        report_id=uuid4() if report else None,
        execution_revision=1,
        created_at=now,
        updated_at=now,
    )

    job = InvestigationJobRecordV1(
        org_id=org_id,
        id=summary.current_job_id,
        repository_id=repo_id,
        investigation_id=inv_id,
        spec_hash="a" * 64,
        operation="investigate_snapshot",
        state="running",
        attempt_count=1,
        max_attempts=3,
        deadline=now,
        created_at=now,
    )

    return InvestigationDetailResponseV2(
        summary=summary,
        job=job,
        report=report,
        claims_count=4,
        evidence_count=7,
        events_count=12,
        available_actions=("cancel",),
        current_support_available=True,
    )


@pytest.mark.asyncio
async def test_mcp_tools_list_includes_investigation_tools() -> None:
    server = ReadOnlyMcpServer()
    req = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    res = await server.handle_request(req)
    assert res["id"] == 1
    tools = {t["name"]: t for t in res["result"]["tools"]}

    assert "get_investigation_status" in tools
    status_tool = tools["get_investigation_status"]
    assert status_tool["inputSchema"]["required"] == ["repository_id", "investigation_id"]

    assert "get_investigation_result" in tools
    result_tool = tools["get_investigation_result"]
    assert result_tool["inputSchema"]["required"] == ["repository_id", "investigation_id"]


@pytest.mark.asyncio
async def test_mcp_tool_call_get_investigation_status() -> None:
    mock_query_svc = AsyncMock()
    org_id = uuid4()
    repo_id = uuid4()
    inv_id = uuid4()
    identity = TenantContext(org_id=org_id, principal_id=uuid4(), role=OrgRole.MEMBER)

    sample_detail = _make_sample_detail(org_id=org_id, repo_id=repo_id, inv_id=inv_id)
    mock_query_svc.detail.return_value = sample_detail

    server = ReadOnlyMcpServer(
        identity=identity,
        investigation_query_service=mock_query_svc,
    )

    req = {
        "jsonrpc": "2.0",
        "id": 10,
        "method": "tools/call",
        "params": {
            "name": "get_investigation_status",
            "arguments": {
                "repository_id": str(repo_id),
                "investigation_id": str(inv_id),
            },
        },
    }
    res = await server.handle_request(req)
    assert res["id"] == 10
    assert not res["result"].get("isError")
    content = json.loads(res["result"]["content"][0]["text"])

    assert content["investigation_id"] == str(inv_id)
    assert content["repository_id"] == str(repo_id)
    assert content["status"] == "running"
    assert content["current_step"] == 3
    assert content["max_steps"] == 10
    assert content["spent_micros"] == 1200000
    assert content["held_micros"] == 500000
    assert content["claims_count"] == 4
    assert content["evidence_count"] == 7
    assert content["events_count"] == 12
    assert content["available_actions"] == ["cancel"]
    assert content["job"]["state"] == "running"
    mock_query_svc.detail.assert_awaited_once_with(identity, repo_id, inv_id)


@pytest.mark.asyncio
async def test_mcp_tool_call_get_investigation_result() -> None:
    mock_query_svc = AsyncMock()
    org_id = uuid4()
    repo_id = uuid4()
    inv_id = uuid4()
    identity = TenantContext(org_id=org_id, principal_id=uuid4(), role=OrgRole.MEMBER)

    sample_report = {
        "verdict": "true_positive",
        "summary": "Verified remote code execution vulnerability.",
        "citations": [
            {"claim_text": "Unvalidated input", "file_path": "server.py", "line_start": 42}
        ],
    }
    sample_detail = _make_sample_detail(
        org_id=org_id,
        repo_id=repo_id,
        inv_id=inv_id,
        status="succeeded",
        verdict="true_positive",
        report=sample_report,
    )
    mock_query_svc.detail.return_value = sample_detail

    server = ReadOnlyMcpServer(
        identity=identity,
        investigation_query_service=mock_query_svc,
    )

    req = {
        "jsonrpc": "2.0",
        "id": 11,
        "method": "tools/call",
        "params": {
            "name": "get_investigation_result",
            "arguments": {
                "repository_id": str(repo_id),
                "investigation_id": str(inv_id),
            },
        },
    }
    res = await server.handle_request(req)
    assert res["id"] == 11
    assert not res["result"].get("isError")
    content = json.loads(res["result"]["content"][0]["text"])

    assert content["investigation_id"] == str(inv_id)
    assert content["repository_id"] == str(repo_id)
    assert content["status"] == "succeeded"
    assert content["verdict"] == "true_positive"
    assert content["verdict_confidence"] == 0.92
    assert content["claims_count"] == 4
    assert content["evidence_count"] == 7
    assert content["report"] == sample_report


@pytest.mark.asyncio
async def test_mcp_investigation_tools_unconfigured_service() -> None:
    identity = TenantContext(org_id=uuid4(), principal_id=uuid4(), role=OrgRole.MEMBER)
    server = ReadOnlyMcpServer(identity=identity)

    req = {
        "jsonrpc": "2.0",
        "id": 12,
        "method": "tools/call",
        "params": {
            "name": "get_investigation_status",
            "arguments": {
                "repository_id": str(uuid4()),
                "investigation_id": str(uuid4()),
            },
        },
    }
    res = await server.handle_request(req)
    assert res["result"]["isError"] is True
    assert "Tool Execution Error" in res["result"]["content"][0]["text"]


@pytest.mark.asyncio
async def test_mcp_investigation_tools_scope_violation() -> None:
    mock_query_svc = AsyncMock()
    mock_query_svc.detail.side_effect = ScopeViolation("Access denied to repository")

    identity = TenantContext(org_id=uuid4(), principal_id=uuid4(), role=OrgRole.MEMBER)
    server = ReadOnlyMcpServer(
        identity=identity,
        investigation_query_service=mock_query_svc,
    )

    req = {
        "jsonrpc": "2.0",
        "id": 13,
        "method": "tools/call",
        "params": {
            "name": "get_investigation_status",
            "arguments": {
                "repository_id": str(uuid4()),
                "investigation_id": str(uuid4()),
            },
        },
    }
    res = await server.handle_request(req)
    assert res["result"]["isError"] is True
    assert "Security/Scope Violation" in res["result"]["content"][0]["text"]


@pytest.mark.asyncio
async def test_mcp_investigation_tools_invalid_uuid() -> None:
    mock_query_svc = AsyncMock()
    identity = TenantContext(org_id=uuid4(), principal_id=uuid4(), role=OrgRole.MEMBER)
    server = ReadOnlyMcpServer(
        identity=identity,
        investigation_query_service=mock_query_svc,
    )

    req = {
        "jsonrpc": "2.0",
        "id": 14,
        "method": "tools/call",
        "params": {
            "name": "get_investigation_status",
            "arguments": {
                "repository_id": "not-a-valid-uuid",
                "investigation_id": str(uuid4()),
            },
        },
    }
    res = await server.handle_request(req)
    assert res["result"]["isError"] is True
    assert "Invalid Parameters" in res["result"]["content"][0]["text"]
