import pytest
from app.adapters.emulator_client import emulator_client


def test_sdk_never_uses_inherited_profile_or_endpoint(monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", "must-not-read-owner")
    monkeypatch.setenv("AWS_CONFIG_FILE", "/forbidden-owner-config")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/forbidden-owner-credentials")
    monkeypatch.setenv("AWS_ENDPOINT_URL", "https://aws.example.com")
    client = emulator_client("s3", "http://127.0.0.1:4567")
    try:
        assert client.meta.endpoint_url == "http://127.0.0.1:4567"
    finally:
        client.close()
    with pytest.raises(ValueError):
        emulator_client("s3", "https://s3.amazonaws.com")
