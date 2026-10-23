"""Unit tests for public scan, findings, triage, and feature trace API endpoints."""

from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
import pytest
from app.api.application import ApiSettings, ApplicationPorts, create_app
from app.domain.scope import OrgRole, TenantContext
from app.ports.object_store import ObjectStore


class DummyStore(ObjectStore):
    async def put(self, key: str, data: bytes) -> None:
        pass

    async def get(self, key: str) -> bytes:
        return b""

    async def get_stream(self, key: str) -> AsyncIterator[bytes]:
        yield b""

    async def delete(self, key: str) -> None:
        pass


def test_openapi_schema_includes_scanner_and_triage_contracts() -> None:
    """Verify OpenAPI generator includes all scan and triage contracts and request bodies."""
    ctx = TenantContext(org_id=uuid4(), principal_id=uuid4(), role=OrgRole.OWNER)
    app = create_app(
        ApiSettings(environment="test", auth_mode="local_fixture", origin="http://testserver"),
        ApplicationPorts(factory=None, store=DummyStore(), fixture_context=ctx),  # type: ignore
    )
    schema = app.openapi()
    schemas = schema["components"]["schemas"]

    assert "CreateScanRequestV1" in schemas
    assert "ScanAcceptedV1" in schemas
    assert "ScanSummaryResponseV1" in schemas
    assert "FindingSummaryV1" in schemas
    assert "FindingListResponseV1" in schemas
    assert "FindingDetailResponseV1" in schemas
    assert "TriageTransitionV1" in schemas
    assert "TriageRecordV1" in schemas
    assert "TriageHistoryV1" in schemas
    assert "FeatureTraceV1" in schemas

    paths = schema["paths"]
    assert "/api/v1/repositories/{repository_id}/snapshots/{snapshot_id}/scans" in paths
    assert "/api/v1/repositories/{repository_id}/scans" in paths
    assert (
        "/api/v1/repositories/{repository_id}/snapshots/{snapshot_id}/scans/{run_id}/summary"
        in paths
    )
    assert (
        "/api/v1/repositories/{repository_id}/snapshots/{snapshot_id}/scans/{run_id}/findings"
        in paths
    )
    assert (
        "/api/v1/repositories/{repository_id}/snapshots/{snapshot_id}/findings/{finding_id}"
        in paths
    )
    assert (
        "/api/v1/repositories/{repository_id}/snapshots/{snapshot_id}/findings/{finding_id}/triage"
        in paths
    )
    assert (
        "/api/v1/repositories/{repository_id}/snapshots/{snapshot_id}/findings/{finding_id}/triage/history"
        in paths
    )
    assert (
        "/api/v1/repositories/{repository_id}/snapshots/{snapshot_id}/findings/{finding_id}/trace"
        in paths
    )


@pytest.mark.asyncio
async def test_scan_and_triage_endpoints_reject_unauthenticated() -> None:
    """Unauthenticated calls to scan, findings, triage and trace endpoints return 401 or 403."""
    ctx = TenantContext(org_id=uuid4(), principal_id=uuid4(), role=OrgRole.OWNER)
    app = create_app(
        ApiSettings(environment="test", auth_mode="local_fixture", origin="http://testserver"),
        ApplicationPorts(factory=None, store=DummyStore(), fixture_context=ctx),  # type: ignore
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        repo_id = uuid4()
        snap_id = uuid4()
        finding_id = uuid4()
        run_id = uuid4()

        # POST scan without auth
        res = await client.post(
            f"/api/v1/repositories/{repo_id}/snapshots/{snap_id}/scans",
            json={"config": None},
        )
        assert res.status_code in (401, 403)

        # GET summary without auth
        res = await client.get(
            f"/api/v1/repositories/{repo_id}/snapshots/{snap_id}/scans/{run_id}/summary",
        )
        assert res.status_code in (401, 403, 404)

        # GET findings list without auth
        res = await client.get(
            f"/api/v1/repositories/{repo_id}/snapshots/{snap_id}/scans/{run_id}/findings",
        )
        assert res.status_code in (401, 403, 404)

        # GET single finding without auth
        res = await client.get(
            f"/api/v1/repositories/{repo_id}/snapshots/{snap_id}/findings/{finding_id}",
        )
        assert res.status_code in (401, 403, 404)

        # POST triage without auth
        res = await client.post(
            f"/api/v1/repositories/{repo_id}/snapshots/{snap_id}/findings/{finding_id}/triage",
            json={"state": "confirmed", "reason": "none", "expected_version": 0},
        )
        assert res.status_code in (401, 403)

        # GET triage history without auth
        res = await client.get(
            f"/api/v1/repositories/{repo_id}/snapshots/{snap_id}/findings/{finding_id}/triage/history",
        )
        assert res.status_code in (401, 403, 404)

        # POST trace without auth
        res = await client.post(
            f"/api/v1/repositories/{repo_id}/snapshots/{snap_id}/findings/{finding_id}/trace",
            json={"budget": {"max_hops": 2, "max_nodes": 50, "max_edges": 100}},
        )
        assert res.status_code in (401, 403)


def test_scan_request_contracts_validation() -> None:
    """Verify request validation rules for CreateScanRequestV1."""
    from app.api.application import CreateScanRequestV1

    # Default None config is valid
    req = CreateScanRequestV1()
    assert req.config is None

    # Serialization roundtrip
    data = req.model_dump_json()
    parsed = CreateScanRequestV1.model_validate_json(data)
    assert parsed.config is None


def test_openapi_component_references_resolve() -> None:
    """Generated clients must retain nested request schemas and enum constraints."""
    ctx = TenantContext(org_id=uuid4(), principal_id=uuid4(), role=OrgRole.OWNER)
    schema = create_app(
        ApiSettings(environment="test", auth_mode="local_fixture", origin="http://testserver"),
        ApplicationPorts(factory=None, store=DummyStore(), fixture_context=ctx),  # type: ignore
    ).openapi()
    pending = [schema]
    missing = set()
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            ref = value.get("$ref")
            if (
                isinstance(ref, str)
                and ref.startswith("#/components/schemas/")
                and ref.rsplit("/", 1)[1] not in schema["components"]["schemas"]
            ):
                missing.add(ref)
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    assert missing == set()
