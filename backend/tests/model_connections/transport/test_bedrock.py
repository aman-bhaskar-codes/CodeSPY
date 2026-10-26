"""Injected asynchronous Converse exchange with no AWS session or credentials."""

import asyncio

import pytest
from app.adapters.model.bedrock import BedrockModelTransport
from app.adapters.model.http_transport import ProviderTransportError

from tests.model_connections.unit.test_codecs import request
from tests.model_connections.unit.test_registry import profile


class SyntheticConverseClient:
    retry_total_attempts = 1

    def __init__(self):
        self.calls = []

    async def converse(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "output": {"message": {"content": [{"text": "answer"}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
        }


@pytest.mark.asyncio
async def test_bedrock_sends_exactly_one_converse_request():
    client = SyntheticConverseClient()
    adapter = BedrockModelTransport(client)
    p = profile("claude_fast")
    prepared = adapter.prepare(request(p), p)
    result = await adapter.invoke(prepared)
    assert result.response.content == "answer"
    assert len(client.calls) == 1
    assert client.calls[0]["modelId"] == p.model_id
    assert client.calls[0]["inferenceConfig"]["maxTokens"] == 100


def test_retrying_client_is_rejected():
    client = SyntheticConverseClient()
    client.retry_total_attempts = 2
    with pytest.raises(ValueError):
        BedrockModelTransport(client)


@pytest.mark.asyncio
async def test_bedrock_total_timeout_has_no_background_call():
    entered = asyncio.Event()
    finished = asyncio.Event()

    class Slow(SyntheticConverseClient):
        async def converse(self, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()

    from app.contracts.model_configuration import ModelTimeoutsV1

    p = profile("claude_fast", timeouts=ModelTimeoutsV1(total_seconds=0.01))
    adapter = BedrockModelTransport(Slow())
    with pytest.raises(ProviderTransportError, match="provider_timeout"):
        await adapter.invoke(adapter.prepare(request(p), p))
    assert entered.is_set() and finished.is_set()
    assert adapter.pending_calls == 0


@pytest.mark.asyncio
async def test_client_error_does_not_expose_vendor_payload():
    class Failed(SyntheticConverseClient):
        async def converse(self, **kwargs):
            raise RuntimeError("private-response-canary")

    p = profile("claude_fast")
    adapter = BedrockModelTransport(Failed())
    with pytest.raises(ProviderTransportError) as error:
        await adapter.invoke(adapter.prepare(request(p), p))
    assert error.value.code == "provider_unavailable"
    assert "private-response-canary" not in str(error.value)


def test_native_client_wrapper_checks_region_and_retry_configuration_without_calls():
    from types import SimpleNamespace

    from app.adapters.model.bedrock import ConfiguredAsyncConverseClient

    class Native(SyntheticConverseClient):
        meta = SimpleNamespace(
            region_name="ap-south-1", config=SimpleNamespace(retries={"total_max_attempts": 1})
        )

    assert ConfiguredAsyncConverseClient(Native()).retry_total_attempts == 1
    client = Native()
    client.meta = SimpleNamespace(
        region_name="us-east-1", config=SimpleNamespace(retries={"total_max_attempts": 1})
    )
    with pytest.raises(ValueError):
        ConfiguredAsyncConverseClient(client)
