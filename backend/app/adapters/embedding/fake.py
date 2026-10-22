"""Deterministic fake embedding adapter for tests and local environments."""

import hashlib
import math
import struct
from uuid import uuid4

from app.contracts.provider_budget import EmbeddingRequestV1, EmbeddingResponseV1


class FakeEmbeddingAdapter:
    """Produces reproducible normalized embeddings derived deterministically from text hashes."""

    def __init__(
        self,
        *,
        fail_mode: str | None = None,
        tokens_per_character: float = 0.25,
    ) -> None:
        self.fail_mode = fail_mode
        self.tokens_per_character = tokens_per_character
        self.calls: list[EmbeddingRequestV1] = []

    async def embed(self, request: EmbeddingRequestV1) -> EmbeddingResponseV1:
        self.calls.append(request)
        if self.fail_mode == "timeout":
            raise TimeoutError("Simulated provider timeout during embedding dispatch")
        if self.fail_mode == "error":
            raise RuntimeError("Simulated provider failure")

        dims = request.dimensions
        h = hashlib.sha256(request.text.encode("utf-8")).digest()
        seed = struct.unpack(">I", h[:4])[0]

        # Generate deterministic values using a simple LCG
        values: list[float] = []
        state = seed
        for _ in range(dims):
            state = (state * 1664525 + 1013904223) & 0xFFFFFFFF
            val = (state / 0xFFFFFFFF) * 2.0 - 1.0
            values.append(val)

        if request.normalize:
            norm = math.sqrt(sum(v * v for v in values)) or 1.0
            values = [v / norm for v in values]

        estimated_tokens = max(1, int(len(request.text) * self.tokens_per_character))

        return EmbeddingResponseV1(
            vector=tuple(values),
            token_count=estimated_tokens,
            model_id=request.model_id,
            provider="fake",
            provider_request_id=f"fake_req_{uuid4().hex[:12]}",
        )
