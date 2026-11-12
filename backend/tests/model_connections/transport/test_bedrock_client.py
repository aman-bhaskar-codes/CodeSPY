"""The client builder pins region, attempts and timeouts without any network access."""

import aioboto3
import pytest
from app.adapters.model.bedrock import ConfiguredAsyncConverseClient
from app.adapters.model.bedrock_client import (
    BEDROCK_REGION,
    bedrock_runtime_config,
    open_bedrock_runtime,
)


def offline_session():
    # Static dummy keys: creating a client never signs or sends anything.
    return aioboto3.Session(
        aws_access_key_id="AKIAIOSFODNN7EXAMPLE",  # gitleaks:allow
        aws_secret_access_key="x" * 40,  # gitleaks:allow
    )


def test_config_pins_region_single_attempt_and_bounds():
    config = bedrock_runtime_config()
    assert config.region_name == BEDROCK_REGION == "ap-south-1"
    assert config.retries["total_max_attempts"] == 1
    assert (config.connect_timeout, config.read_timeout, config.max_pool_connections) == (5, 60, 4)


async def test_builder_yields_a_client_the_converse_wrapper_accepts():
    async with open_bedrock_runtime(offline_session()) as client:
        assert client.meta.region_name == "ap-south-1"
        assert client.meta.config.retries["total_max_attempts"] == 1
        assert ConfiguredAsyncConverseClient(client).retry_total_attempts == 1
        assert "bedrock-runtime.ap-south-1.amazonaws.com" in client.meta.endpoint_url


async def test_wrapper_still_refuses_a_foreign_region_client():
    async with offline_session().client("bedrock-runtime", region_name="us-east-1") as client:
        with pytest.raises(ValueError, match="Mumbai"):
            ConfiguredAsyncConverseClient(client)
