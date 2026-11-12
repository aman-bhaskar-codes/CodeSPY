"""Owner-run Bedrock smoke check (makes two small paid calls).

    CODESPY_LIVE_BEDROCK=1 AWS_PROFILE=<profile> uv run --project backend \
        python scripts/bedrock_smoke.py --converse-model <model-or-inference-profile-id>

Sends one tiny Converse prompt and one Titan V2 embedding to ap-south-1 using the credentials of
the named AWS profile. Prints model ids, token counts, latency and vector size only: never the
prompt, the completion, credentials or the response body. Refuses to run without the opt-in.
"""

import argparse
import asyncio
import math
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "backend")]

PROMPT = "Reply with the single word: ok"
MAX_OUTPUT_TOKENS = 16
EMBED_TEXT = "hello"


async def run(profile: str, converse_model: str, skip_embedding: bool) -> int:
    import aioboto3
    from app.adapters.embedding.titan import MODEL_ID, AmazonTitanEmbeddingAdapter
    from app.adapters.model.bedrock import ConfiguredAsyncConverseClient
    from app.adapters.model.bedrock_client import open_bedrock_runtime
    from app.contracts.provider_budget import EmbeddingRequestV1

    session = aioboto3.Session(profile_name=profile)
    async with open_bedrock_runtime(session) as client:
        started = time.perf_counter()
        reply = await ConfiguredAsyncConverseClient(client).converse(
            modelId=converse_model,
            messages=[{"role": "user", "content": [{"text": PROMPT}]}],
            inferenceConfig={"maxTokens": MAX_OUTPUT_TOKENS, "temperature": 0},
        )
        usage = reply.get("usage", {})
        print(
            f"converse ok model={converse_model} stop={reply.get('stopReason')} "
            f"in={usage.get('inputTokens')} out={usage.get('outputTokens')} "
            f"ms={(time.perf_counter() - started) * 1000:.0f}"
        )
        if skip_embedding:
            return 0
        started = time.perf_counter()
        embedded = await AmazonTitanEmbeddingAdapter(client).embed(
            EmbeddingRequestV1(
                text=EMBED_TEXT,
                provider="aws_bedrock",
                model_id=MODEL_ID,
                dimensions=1024,
            )
        )
        norm = math.sqrt(sum(v * v for v in embedded.vector))
        print(
            f"embedding ok model={embedded.model_id} dims={len(embedded.vector)} "
            f"tokens={embedded.token_count} norm={norm:.3f} "
            f"ms={(time.perf_counter() - started) * 1000:.0f}"
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--converse-model", required=True, help="model or inference-profile id"
    )
    parser.add_argument("--skip-embedding", action="store_true")
    args = parser.parse_args()
    if os.environ.get("CODESPY_LIVE_BEDROCK") != "1":
        print(
            "refusing to run: set CODESPY_LIVE_BEDROCK=1 to make two small paid Bedrock calls"
        )
        return 2
    profile = os.environ.get("AWS_PROFILE")
    if not profile:
        print("refusing to run: set AWS_PROFILE to the profile you want to use")
        return 2
    print(f"region=ap-south-1 profile={profile} (about two calls, a few tokens each)")
    return asyncio.run(run(profile, args.converse_model, args.skip_embedding))


if __name__ == "__main__":
    raise SystemExit(main())
