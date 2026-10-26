"""Bounded local HTTPX exchanges; synthetic credentials and injected transport only."""

import asyncio
import json

import httpx
import pytest
from app.adapters.model.gemini import GeminiContentCodec
from app.adapters.model.http_transport import HTTPModelTransport, ProviderTransportError
from app.adapters.model.openai import OpenAIResponsesCodec
from pydantic import SecretStr

from tests.model_connections.unit.test_codecs import request
from tests.model_connections.unit.test_registry import profile


async def synthetic_secret(reference):
    assert reference == "SYNTHETIC_KEY"
    return SecretStr("synthetic-secret-canary")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,expected",
    [
        (401, "provider_authentication"),
        (429, "provider_rate_limited"),
        (500, "provider_unavailable"),
        (302, "provider_redirect_rejected"),
    ],
)
async def test_failed_requests_are_not_retried(status, expected):
    calls = []

    def handler(req):
        calls.append(req)
        assert req.headers["authorization"] == "Bearer synthetic-secret-canary"
        assert req.url.host == "api.openai.com"
        return httpx.Response(status, json={"error": "private-response-canary"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as client:
        adapter = HTTPModelTransport(OpenAIResponsesCodec(), client, synthetic_secret)
        p = profile()
        with pytest.raises(ProviderTransportError) as error:
            await adapter.invoke(adapter.prepare(request(p), p))
        assert error.value.code == expected
        assert "private-response-canary" not in str(error.value)
        assert "synthetic-secret-canary" not in str(error.value)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_response_size_limit_and_total_deadline():
    async def handler(req):
        await asyncio.sleep(0.05)
        return httpx.Response(200, content=b"x" * 100)

    p = profile(response_byte_limit=10)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = HTTPModelTransport(OpenAIResponsesCodec(), client, synthetic_secret)
        with pytest.raises(ProviderTransportError, match="provider_response_too_large"):
            await adapter.invoke(adapter.prepare(request(p), p))
        from app.contracts.model_configuration import ModelTimeoutsV1

        p = profile(timeouts=ModelTimeoutsV1(total_seconds=0.01))
        with pytest.raises(ProviderTransportError, match="provider_timeout"):
            await adapter.invoke(adapter.prepare(request(p), p))


@pytest.mark.asyncio
async def test_direct_gemini_uses_header_and_encoded_model_path():
    def handler(req):
        assert req.url.host == "generativelanguage.googleapis.com"
        assert req.headers["x-goog-api-key"] == "synthetic-secret-canary"
        assert "key=" not in str(req.url)
        body = json.loads(req.content)
        assert body["generationConfig"]["candidateCount"] == 1
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"text": "answer"}]}, "finishReason": "STOP"}
                ],
                "usageMetadata": {
                    "promptTokenCount": 10,
                    "candidatesTokenCount": 5,
                    "thoughtsTokenCount": 0,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        p = profile("gemini_general", api_version="v1beta")
        adapter = HTTPModelTransport(GeminiContentCodec(), client, synthetic_secret)
        result = await adapter.invoke(adapter.prepare(request(p), p))
        assert result.response.content == "answer"


@pytest.mark.asyncio
async def test_cancellation_propagates_and_releases_capacity():
    entered = asyncio.Event()

    async def handler(req):
        entered.set()
        await asyncio.Event().wait()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        p = profile()
        adapter = HTTPModelTransport(OpenAIResponsesCodec(), client, synthetic_secret)
        task = asyncio.create_task(adapter.invoke(adapter.prepare(request(p), p)))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert adapter.pending_calls == 0


@pytest.mark.asyncio
async def test_credential_failure_is_sanitized_before_dispatch():
    async def broken(reference):
        raise RuntimeError("private-credential-canary")

    def never_called(req):
        raise AssertionError("unexpected network dispatch")

    async with httpx.AsyncClient(transport=httpx.MockTransport(never_called)) as client:
        p = profile()
        adapter = HTTPModelTransport(OpenAIResponsesCodec(), client, broken)
        with pytest.raises(ProviderTransportError) as error:
            await adapter.invoke(adapter.prepare(request(p), p))
        assert error.value.code == "provider_credentials_unavailable"
        assert error.value.possibly_dispatched is False
        assert "private-credential-canary" not in str(error.value)


@pytest.mark.asyncio
async def test_dispatch_marker_runs_after_credentials_and_before_network():
    sequence = []

    async def secret(ref):
        sequence.append("credentials")
        return SecretStr("synthetic")

    async def marker():
        sequence.append("committed")

    def handler(req):
        sequence.append("network")
        return httpx.Response(429)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        p = profile()
        adapter = HTTPModelTransport(OpenAIResponsesCodec(), client, secret)
        with pytest.raises(ProviderTransportError):
            await adapter.invoke(adapter.prepare(request(p), p), before_dispatch=marker)
    assert sequence == ["credentials", "committed", "network"]
