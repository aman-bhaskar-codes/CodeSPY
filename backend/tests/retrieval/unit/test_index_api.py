"""Public indexing contracts exclude caller-controlled runtime configuration."""

import pytest
from pydantic import ValidationError


def test_public_index_start_accepts_only_request_identity() -> None:
    from app.contracts.indexing import IndexStartRequestV1

    assert IndexStartRequestV1(request_key="build-one").request_key == "build-one"
    for body in (
        {"request_key": "build", "config": {}},
        {"request_key": "job:reserved"},
        {"request_key": "a" * 129},
    ):
        with pytest.raises(ValidationError):
            IndexStartRequestV1.model_validate(body)


def test_disabled_ports_have_no_index_configuration() -> None:
    from app.api.application import ApplicationPorts

    assert ApplicationPorts.__dataclass_fields__["index_config"].default is None
    assert ApplicationPorts.__dataclass_fields__["index_builder"].default is None


def test_openapi_exports_public_index_start_and_retry_bodies() -> None:
    from unittest.mock import MagicMock

    from app.api.application import ApiSettings, ApplicationPorts, create_app

    app = create_app(
        ApiSettings(environment="test", origin="http://testserver"),
        ApplicationPorts(MagicMock(), MagicMock()),
    )
    schema = app.openapi()
    assert set(schema["components"]["schemas"]["IndexStartRequestV1"]["properties"]) == {
        "request_key"
    }
    for path in (
        "/api/v1/repositories/{repository_id}/snapshots/{snapshot_id}/indexes",
        "/api/v1/repositories/{repository_id}/snapshots/{snapshot_id}/index-jobs/{job_id}/retry",
    ):
        body = schema["paths"][path]["post"]["requestBody"]
        assert body["required"] is True
        assert (
            body["content"]["application/json"]["schema"]["$ref"]
            == "#/components/schemas/IndexStartRequestV1"
        )
