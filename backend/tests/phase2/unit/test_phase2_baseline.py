from inspect import signature

from app.services.persistence import PersistenceService


def test_phase1_public_signature_and_shared_helpers():
    assert list(signature(PersistenceService.create_snapshot).parameters) == [
        "self",
        "ctx",
        "data",
        "files",
    ]
    for method in [
        "_create_snapshot_in_session",
        "_create_run_in_session",
        "_transition_in_session",
        "_persist_result_in_session",
        "_resolve_snapshot_files_in_session",
    ]:
        assert hasattr(PersistenceService, method), method


def test_locked_authlib_and_conditional_write_capabilities():
    import inspect

    from authlib.integrations.httpx_client import AsyncOAuth2Client
    from botocore.session import Session

    assert hasattr(AsyncOAuth2Client, "create_authorization_url")
    assert "url" in inspect.signature(AsyncOAuth2Client.fetch_token).parameters
    model = Session().get_service_model("s3").operation_model("PutObject").input_shape.members
    assert {"IfNoneMatch", "ChecksumSHA256"} <= model.keys()
