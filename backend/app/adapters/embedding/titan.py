"""Amazon Titan Text Embeddings V2 over an explicitly injected async Bedrock client.

Request body:  {"inputText": str, "dimensions": int, "normalize": bool}
Response body: {"embedding": [float, ...], "inputTextTokenCount": int}

The adapter never creates a client, discovers credentials, retries or simulates. A missing or
foreign-region client is refused at construction; every failure is reported as a stable code with
``possibly_dispatched`` telling the accounting layer whether the request may have left the process.
"""

import asyncio
import json
import math
from typing import Any, Protocol

from app.adapters.model.http_transport import ProviderTransportError
from app.contracts.provider_budget import EmbeddingRequestV1, EmbeddingResponseV1
from app.ports.embedding import EmbeddingPort

MODEL_ID = "amazon.titan-embed-text-v2:0"
PROVIDER = "aws_bedrock"
REGION = "ap-south-1"
SUPPORTED_DIMENSIONS = (256, 512, 1024)
MAX_INPUT_BYTES = 200_000  # a 8k-token input is far below this; larger input is refused locally
MAX_RESPONSE_BYTES = 262_144


class AsyncInvokeClientPort(Protocol):
    async def invoke_model(self, **kwargs: Any) -> dict[str, Any]: ...


class AmazonTitanEmbeddingAdapter(EmbeddingPort):
    def __init__(
        self,
        client: Any,
        *,
        timeout_seconds: float = 30.0,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        if client is None:
            raise ValueError("an owner-created Bedrock client is required")
        meta = client.meta
        if meta.region_name != REGION or meta.config.retries.get("total_max_attempts") != 1:
            raise ValueError("Titan client requires Mumbai and exactly one total attempt")
        if not 0 < timeout_seconds <= 120 or not 0 < max_response_bytes <= 4_194_304:
            raise ValueError("timeout and response bounds are out of range")
        self._client: AsyncInvokeClientPort = client
        self._timeout = timeout_seconds
        self._max_response = max_response_bytes

    async def embed(self, request: EmbeddingRequestV1) -> EmbeddingResponseV1:
        if request.provider != PROVIDER or request.model_id != MODEL_ID:
            raise ValueError("request is not for the Titan V2 model on aws_bedrock")
        if request.dimensions not in SUPPORTED_DIMENSIONS:
            raise ValueError(f"unsupported dimension; supported: {SUPPORTED_DIMENSIONS}")
        text = request.text
        if len(text.encode()) > MAX_INPUT_BYTES:
            raise ValueError("embedding input exceeds the local size bound")
        body = json.dumps(
            {"inputText": text, "dimensions": request.dimensions, "normalize": request.normalize},
            allow_nan=False,
        )
        sent = False
        try:
            async with asyncio.timeout(self._timeout):
                sent = True
                response = await self._client.invoke_model(
                    modelId=MODEL_ID,
                    body=body,
                    contentType="application/json",
                    accept="application/json",
                )
                raw = await response["body"].read()
        except TimeoutError:
            raise ProviderTransportError("provider_timeout", dispatched=sent) from None
        except Exception:
            raise ProviderTransportError("provider_unavailable", dispatched=sent) from None
        if len(raw) > self._max_response:
            raise ProviderTransportError("provider_response_too_large")
        try:
            payload = json.loads(raw)
            vector = tuple(float(x) for x in payload["embedding"])
            tokens = int(payload["inputTextTokenCount"])
            request_id = response.get("ResponseMetadata", {}).get("RequestId")
        except (ValueError, KeyError, TypeError):
            raise ProviderTransportError("unsupported_protocol") from None
        if (
            len(vector) != request.dimensions
            or tokens < 0
            or not all(math.isfinite(v) for v in vector)
            or not any(vector)
        ):
            raise ProviderTransportError("unsupported_protocol")
        return EmbeddingResponseV1(
            vector=vector,
            token_count=tokens,
            model_id=MODEL_ID,
            provider=PROVIDER,
            provider_request_id=request_id if isinstance(request_id, str) else None,
        )
