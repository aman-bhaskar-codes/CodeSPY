"""Amazon Titan Text Embeddings V2 adapter adhering to Bedrock JSON contracts."""

import json
from typing import Any
from uuid import uuid4

from app.contracts.provider_budget import EmbeddingRequestV1, EmbeddingResponseV1
from app.ports.embedding import EmbeddingPort


class AmazonTitanEmbeddingAdapter(EmbeddingPort):
    """Adapter for Amazon Titan Text Embeddings V2 (amazon.titan-embed-text-v2:0).

    Adheres strictly to the AWS Bedrock invoke_model JSON contract:
      Input payload:  {"inputText": str, "dimensions": int, "normalize": bool}
      Output payload: {"embedding": list[float], "inputTextTokenCount": int}

    In offline / test environments without active AWS credentials, defaults to
    deterministic simulation so tests remain self-contained without paid API calls.
    """

    MODEL_ID = "amazon.titan-embed-text-v2:0"
    SUPPORTED_DIMENSIONS = (256, 512, 1024)

    def __init__(
        self,
        *,
        bedrock_client: Any | None = None,
        simulate_offline: bool = False,
    ) -> None:
        self.bedrock_client = bedrock_client
        self.simulate_offline = simulate_offline or (bedrock_client is None)
        self.invocations: list[dict[str, Any]] = []

    async def embed(self, request: EmbeddingRequestV1) -> EmbeddingResponseV1:
        if request.dimensions not in self.SUPPORTED_DIMENSIONS:
            raise ValueError(
                f"Unsupported dimension {request.dimensions} for Titan V2. "
                f"Supported: {self.SUPPORTED_DIMENSIONS}"
            )

        payload = {
            "inputText": request.text,
            "dimensions": request.dimensions,
            "normalize": request.normalize,
        }
        self.invocations.append(payload)

        if not self.simulate_offline and self.bedrock_client is not None:
            # Live invocation path (only reachable when explicitly configured with client)
            response = self.bedrock_client.invoke_model(
                modelId=request.model_id or self.MODEL_ID,
                body=json.dumps(payload),
                contentType="application/json",
                accept="application/json",
            )
            body = json.loads(response["body"].read().decode("utf-8"))
            vector = tuple(float(x) for x in body["embedding"])
            token_count = int(body.get("inputTextTokenCount", 0))
            resp_meta = response.get("ResponseMetadata", {})
            req_id = resp_meta.get("RequestId", f"titan_{uuid4().hex[:12]}")
            return EmbeddingResponseV1(
                vector=vector,
                token_count=token_count,
                model_id=request.model_id or self.MODEL_ID,
                provider="aws_bedrock",
                provider_request_id=req_id,
            )

        # Deterministic simulation path for test/offline execution
        import hashlib
        import math
        import struct

        h = hashlib.sha256(request.text.encode("utf-8")).digest()
        seed = struct.unpack(">I", h[:4])[0]
        values: list[float] = []
        state = seed
        for _ in range(request.dimensions):
            state = (state * 1664525 + 1013904223) & 0xFFFFFFFF
            val = (state / 0xFFFFFFFF) * 2.0 - 1.0
            values.append(val)

        if request.normalize:
            norm = math.sqrt(sum(v * v for v in values)) or 1.0
            values = [v / norm for v in values]

        est_tokens = max(1, len(request.text) // 4)

        return EmbeddingResponseV1(
            vector=tuple(values),
            token_count=est_tokens,
            model_id=request.model_id or self.MODEL_ID,
            provider="aws_bedrock_simulated",
            provider_request_id=f"sim_titan_{uuid4().hex[:12]}",
        )
