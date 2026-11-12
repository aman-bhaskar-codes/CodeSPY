"""Titan adapter: strict request/response handling with a synthetic async client."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from app.adapters.embedding.titan import AmazonTitanEmbeddingAdapter
from app.adapters.model.http_transport import ProviderTransportError
from app.contracts.provider_budget import EmbeddingRequestV1

MODEL = "amazon.titan-embed-text-v2:0"


class Body:
    def __init__(self, data: bytes):
        self.data = data

    async def read(self) -> bytes:
        return self.data


class Client:
    def __init__(self, *, vector=None, tokens=3, raw=None, region="ap-south-1", attempts=1):
        self.meta = SimpleNamespace(
            region_name=region, config=SimpleNamespace(retries={"total_max_attempts": attempts})
        )
        self.vector = vector if vector is not None else [0.6, 0.8] + [0.0] * 254
        self.tokens, self.raw, self.calls = tokens, raw, []
        self.fail: Exception | None = None
        self.delay = 0.0

    async def invoke_model(self, **kwargs):
        self.calls.append(kwargs)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise self.fail
        raw = (
            self.raw
            or json.dumps({"embedding": self.vector, "inputTextTokenCount": self.tokens}).encode()
        )
        return {"body": Body(raw), "ResponseMetadata": {"RequestId": "req-1"}}


def request(**overrides):
    values = {"text": "hello", "provider": "aws_bedrock", "model_id": MODEL, "dimensions": 256}
    return EmbeddingRequestV1(**{**values, **overrides})


async def test_valid_call_sends_the_documented_body_once_and_returns_the_vector():
    client = Client()
    result = await AmazonTitanEmbeddingAdapter(client).embed(request())
    assert len(client.calls) == 1 and client.calls[0]["modelId"] == MODEL
    assert json.loads(client.calls[0]["body"]) == {
        "inputText": "hello",
        "dimensions": 256,
        "normalize": True,
    }
    assert (result.provider, result.model_id, result.token_count) == ("aws_bedrock", MODEL, 3)
    assert result.provider_request_id == "req-1" and len(result.vector) == 256


def test_no_client_means_no_silent_simulation_and_foreign_clients_are_refused():
    with pytest.raises(ValueError, match="required"):
        AmazonTitanEmbeddingAdapter(None)
    with pytest.raises(ValueError, match="Mumbai"):
        AmazonTitanEmbeddingAdapter(Client(region="us-east-1"))
    with pytest.raises(ValueError, match="Mumbai"):
        AmazonTitanEmbeddingAdapter(Client(attempts=3))


@pytest.mark.parametrize(
    "overrides",
    [{"provider": "fake"}, {"model_id": "other"}, {"dimensions": 300}],
)
async def test_wrong_provider_model_or_dimension_never_reaches_the_client(overrides):
    client = Client()
    with pytest.raises(ValueError):
        await AmazonTitanEmbeddingAdapter(client).embed(request(**overrides))
    assert client.calls == []


async def test_oversized_input_is_refused_before_sending():
    client = Client()
    with pytest.raises(ValueError, match="size bound"):
        await AmazonTitanEmbeddingAdapter(client).embed(request(text="x" * 200_001))
    assert client.calls == []


@pytest.mark.parametrize(
    "vector",
    [[0.0] * 256, [float("nan")] + [0.1] * 255, [0.1] * 255],
)
async def test_degenerate_vectors_are_rejected(vector):
    client = Client()
    client.raw = (
        json.dumps({"embedding": vector, "inputTextTokenCount": 2})
        .encode()
        .replace(b"NaN", b"null")
    )
    with pytest.raises(ProviderTransportError) as error:
        await AmazonTitanEmbeddingAdapter(client).embed(request())
    assert error.value.code == "unsupported_protocol"


async def test_malformed_and_oversized_responses_use_stable_codes():
    for raw, code in ((b"not json", "unsupported_protocol"), (b"{}", "unsupported_protocol")):
        with pytest.raises(ProviderTransportError) as error:
            await AmazonTitanEmbeddingAdapter(Client(raw=raw)).embed(request())
        assert error.value.code == code
    with pytest.raises(ProviderTransportError) as error:
        await AmazonTitanEmbeddingAdapter(Client(raw=b"x" * 100), max_response_bytes=10).embed(
            request()
        )
    assert error.value.code == "provider_response_too_large"


async def test_failures_and_timeouts_are_sanitized_and_marked_dispatched():
    client = Client()
    client.fail = RuntimeError("secret-token in provider body")
    with pytest.raises(ProviderTransportError) as error:
        await AmazonTitanEmbeddingAdapter(client).embed(request())
    assert error.value.code == "provider_unavailable" and error.value.possibly_dispatched
    assert "secret" not in str(error.value)

    slow = Client()
    slow.delay = 1.0
    with pytest.raises(ProviderTransportError) as error:
        await AmazonTitanEmbeddingAdapter(slow, timeout_seconds=0.05).embed(request())
    assert error.value.code == "provider_timeout" and error.value.possibly_dispatched
