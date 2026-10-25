"""Index status tool uses trusted identity and the existing durable status service."""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from app.domain.scope import OrgRole, TenantContext
from app.mcp.server import ReadOnlyMcpServer


@pytest.mark.asyncio
async def test_status_tool_has_no_identity_selector_and_forwards_durable_status():
    ctx = TenantContext(org_id=uuid4(), principal_id=uuid4(), role=OrgRole.MEMBER)
    repo, snapshot, job = uuid4(), uuid4(), uuid4()
    payload = {
        "job": {"id": str(job), "state": "running"},
        "receipt": None,
        "can_cancel": True,
        "can_retry": False,
    }

    class Status:
        async def get(self, identity, repository_id, snapshot_id, job_id):
            assert (identity, repository_id, snapshot_id, job_id) == (ctx, repo, snapshot, job)
            return SimpleNamespace(model_dump=lambda **kwargs: payload)

    server = ReadOnlyMcpServer(identity=ctx, index_service=Status())
    definition = next(t for t in server.get_tool_definitions() if t["name"] == "get_index_status")
    assert set(definition["inputSchema"]["properties"]) == {
        "repository_id",
        "snapshot_id",
        "job_id",
    }
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "get_index_status",
            "arguments": {
                "repository_id": str(repo),
                "snapshot_id": str(snapshot),
                "job_id": str(job),
            },
        },
    }
    result = await server.handle_request(request)
    assert result["result"]["isError"] is False
    assert json.loads(result["result"]["content"][0]["text"]) == payload
    request["params"]["arguments"]["org_id"] = str(uuid4())
    rejected = await server.handle_request(request)
    assert rejected["result"]["isError"] is True


@pytest.mark.asyncio
async def test_status_tool_does_not_authorize_mutation_or_work_without_identity():
    server = ReadOnlyMcpServer()
    req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "get_index_status",
            "arguments": {
                "repository_id": str(uuid4()),
                "snapshot_id": str(uuid4()),
                "job_id": str(uuid4()),
            },
        },
    }
    assert (await server.handle_request(req))["result"]["isError"] is True
    for name in ("create_index", "retry_index", "cancel_index"):
        req["params"]["name"] = name
        assert (await server.handle_request(req))["result"]["isError"] is True
