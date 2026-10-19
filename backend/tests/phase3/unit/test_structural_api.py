"""Unit tests for Phase 3 Structural Analysis API endpoints and schema."""

from uuid import uuid4

import pytest
from app.api.application import ApiSettings, ApplicationPorts, create_app
from app.contracts.structure import StructuralAnalysisRequestV1
from app.domain.scope import OrgRole, TenantContext


def test_openapi_schema_includes_structural_contracts() -> None:
    """Verify OpenAPI generator includes Phase 3 contracts and request bodies."""
    ctx = TenantContext(org_id=uuid4(), principal_id=uuid4(), role=OrgRole.OWNER)
    app = create_app(
        ApiSettings(environment="test", auth_mode="local_fixture", origin="http://testserver"),
        ApplicationPorts(factory=lambda: None, store=None, fixture_context=ctx),  # type: ignore[arg-type]
    )
    schema = app.openapi()
    schemas = schema["components"]["schemas"]
    assert "StructuralAnalysisRequestV1" in schemas
    assert "AnalysisAcceptedV1" in schemas

    analysis_post = schema["paths"]["/api/v1/repositories/{repository_id}/analyses"]["post"]
    assert analysis_post["requestBody"]["content"]["application/json"]["schema"]["$ref"] == (
        "#/components/schemas/StructuralAnalysisRequestV1"
    )

    report_path = "/api/v1/repositories/{repository_id}/analyses/{run_id}/report"
    assert schema["paths"][report_path]["get"] is not None

    export_path = "/api/v1/repositories/{repository_id}/analyses/{run_id}/export"
    assert schema["paths"][export_path]["get"] is not None


def test_structural_request_validation() -> None:
    """Test StructuralAnalysisRequestV1 parsing and validation."""
    snap_id = uuid4()
    req = StructuralAnalysisRequestV1(snapshot_id=snap_id)
    assert req.snapshot_id == snap_id
    assert req.include_prefixes == ()
    assert req.profile == "structural-small-v1"

    # Serialization round trip
    data = req.model_dump_json()
    parsed = StructuralAnalysisRequestV1.model_validate_json(data)
    assert parsed.snapshot_id == snap_id
    assert parsed.profile == "structural-small-v1"

    # Custom prefixes
    req_with_prefixes = StructuralAnalysisRequestV1(
        snapshot_id=snap_id,
        include_prefixes=("src/backend", "src/frontend/"),
    )
    assert req_with_prefixes.include_prefixes == ("src/backend", "src/frontend")

    # Invalid prefix should fail validation
    with pytest.raises(ValueError, match="relative POSIX prefix required"):
        StructuralAnalysisRequestV1(
            snapshot_id=snap_id,
            include_prefixes=("../escape",),
        )


@pytest.mark.asyncio
async def test_api_structural_endpoints_unauthenticated() -> None:
    """Test unauthenticated calls to structural endpoints return 401 or 403."""
    import httpx

    ctx = TenantContext(org_id=uuid4(), principal_id=uuid4(), role=OrgRole.OWNER)
    app = create_app(
        ApiSettings(environment="test", auth_mode="local_fixture", origin="http://testserver"),
        ApplicationPorts(factory=lambda: None, store=None, fixture_context=ctx),  # type: ignore[arg-type]
    )

    repo_id = uuid4()
    run_id = uuid4()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        # POST without auth/csrf
        res_post = await client.post(
            f"/api/v1/repositories/{repo_id}/analyses",
            json={"snapshot_id": str(uuid4())},
        )
        assert res_post.status_code in (401, 403)

        # GET report without valid session cookie
        res_get = await client.get(
            f"/api/v1/repositories/{repo_id}/analyses/{run_id}/report",
        )
        assert res_get.status_code in (401, 403, 404)

        # GET export without valid session cookie
        res_export = await client.get(
            f"/api/v1/repositories/{repo_id}/analyses/{run_id}/export?format=json",
        )
        assert res_export.status_code in (401, 403, 404)
