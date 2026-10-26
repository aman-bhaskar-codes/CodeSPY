"""Authenticated transport wiring uses route snapshot identity and explicit model availability."""

from types import SimpleNamespace

from app.contracts.memory import MemoryQueryResponseV1
from app.services.investigation_checkpointer import FencedCheckpointer
from app.services.memory_service import MemoryService

from tests.phase4.unit.test_findings_authorization import authority as authority_fixture
from tests.phase4.unit.test_scan_status_api import status_client as status_client_fixture

authority = authority_fixture
status_client = status_client_fixture


async def test_memory_query_receives_route_snapshot(authority, status_client, monkeypatch):
    async def query(self, request):
        assert request.snapshot_id == authority.snapshot_id
        return MemoryQueryResponseV1(
            org_id=authority.ctx.org_id,
            repository_id=authority.repository_id,
            query=request.query,
            total_candidates=0,
            items=(),
        )

    monkeypatch.setattr(MemoryService, "query_memory", query)
    result = await status_client.get(
        f"/api/v1/repositories/{authority.repository_id}/snapshots/{authority.snapshot_id}/memories?query=architecture"
    )
    assert result.status_code == 200


async def test_unconfigured_investigation_does_not_acquire_lease_or_simulate_success(
    authority, status_client, monkeypatch
):
    async def summary(*args):
        return SimpleNamespace(repository_id=authority.repository_id)

    async def claim(*args, **kwargs):
        raise AssertionError("unconfigured inference must not claim work")

    monkeypatch.setattr(FencedCheckpointer, "get_investigation_summary", summary)
    monkeypatch.setattr(FencedCheckpointer, "acquire_lease", claim)
    result = await status_client.post(
        f"/api/v1/repositories/{authority.repository_id}/investigations/{authority.run_id}/execute",
        json={},
    )
    assert result.status_code == 503
    assert result.json()["code"] == "inference_unavailable"


async def test_feedback_cannot_fabricate_repository_id_as_memory_snapshot(
    authority, status_client, monkeypatch
):
    async def investigation(*args):
        return {"repository_id": str(authority.repository_id)}

    async def write(*args, **kwargs):
        raise AssertionError("snapshot-unbound memory request must fail before writing feedback")

    monkeypatch.setattr(FencedCheckpointer, "get_investigation", investigation)
    monkeypatch.setattr(FencedCheckpointer, "record_feedback", write)
    result = await status_client.post(
        f"/api/v1/repositories/{authority.repository_id}/investigations/{authority.run_id}/feedback",
        json={"feedback_type": "agree", "notes": "synthetic feedback", "save_to_memory": True},
    )
    assert result.status_code == 409
    assert result.json()["code"] == "memory_snapshot_unbound"


async def test_hybrid_query_bounds_are_request_validation(authority, status_client, monkeypatch):
    from app.contracts.search import HybridSearchResponseV1
    from app.services.hybrid_search import HybridSearchService

    async def search(self, request):
        assert len(request.query) <= 256
        return HybridSearchResponseV1(
            scope=request.scope,
            query=request.query,
            total_candidates=0,
            hits=(),
            dense_availability="index_unavailable",
            coverage_status="partial",
        )

    monkeypatch.setattr(HybridSearchService, "search_hybrid", search)
    route = f"/api/v1/repositories/{authority.repository_id}/snapshots/{authority.snapshot_id}"
    accepted = await status_client.post(route + "/search/hybrid", json={"query": "x" * 256})
    assert accepted.status_code == 200
    rejected = await status_client.post(route + "/search/hybrid", json={"query": "x" * 257})
    assert rejected.status_code == 422
    context = await status_client.post(route + "/context/assemble", json={"query": "x" * 257})
    assert context.status_code == 422


async def test_memory_query_values_fail_at_request_boundary(authority, status_client, monkeypatch):
    async def query(*args):
        raise AssertionError("invalid query parameters must not reach memory service")

    monkeypatch.setattr(MemoryService, "query_memory", query)
    route = (
        f"/api/v1/repositories/{authority.repository_id}/snapshots/{authority.snapshot_id}/memories"
    )
    for params in (
        {"query": "x" * 257},
        {"query": "architecture", "limit": 101},
        {"query": "architecture", "limit": 0},
    ):
        response = await status_client.get(route, params=params)
        assert response.status_code == 422


async def test_exact_route_preserves_set_pin_and_rebinds_identity(
    authority, status_client, monkeypatch
):
    from uuid import uuid4

    from app.contracts.search import SearchMode, SearchResponseV1
    from app.services.lexical_search import LexicalSearchService

    selected = uuid4()

    async def search(self, request):
        assert request.scope.chunk_set_id == selected
        assert request.scope.org_id == authority.ctx.org_id
        assert request.scope.principal_id == authority.ctx.principal_id
        assert request.scope.snapshot_id == authority.snapshot_id
        return SearchResponseV1(
            scope=request.scope,
            mode=SearchMode.EXACT,
            query=request.query,
            total_candidates=0,
            hits=(),
            coverage_status="partial",
        )

    monkeypatch.setattr(LexicalSearchService, "search_exact", search)
    route = (
        f"/api/v1/repositories/{authority.repository_id}"
        f"/snapshots/{authority.snapshot_id}/search/exact"
    )
    response = await status_client.post(
        route,
        json={
            "query": "a.py",
            "scope": {
                "org_id": str(uuid4()),
                "repository_id": str(uuid4()),
                "snapshot_id": str(uuid4()),
                "principal_id": str(uuid4()),
                "chunk_set_id": str(selected),
            },
        },
    )
    assert response.status_code == 200
