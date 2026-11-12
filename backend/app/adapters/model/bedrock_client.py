"""Owner-side construction of the Bedrock Runtime client.

The composition root calls this with a session the owner created; library code never reads
environment variables or discovers credentials. The client is pinned to Mumbai, makes exactly
one attempt per call and has bounded timeouts and connections, which is what
``ConfiguredAsyncConverseClient`` and the Titan adapter verify.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from botocore.config import Config

BEDROCK_REGION = "ap-south-1"
CONNECT_SECONDS = 5
READ_SECONDS = 60
MAX_CONNECTIONS = 4


def bedrock_runtime_config() -> Config:
    return Config(
        region_name=BEDROCK_REGION,
        retries={"total_max_attempts": 1, "mode": "standard"},
        connect_timeout=CONNECT_SECONDS,
        read_timeout=READ_SECONDS,
        max_pool_connections=MAX_CONNECTIONS,
        user_agent_extra="codespy-bedrock",
    )


@asynccontextmanager
async def open_bedrock_runtime(session: Any) -> AsyncIterator[Any]:
    """Yield an ``aioboto3`` ``bedrock-runtime`` client created from an owner-built session.

    ``session`` is an ``aioboto3.Session`` configured by the caller (profile, role or static
    test keys). The client is closed when the context exits; no endpoint override is accepted.
    """
    async with session.client("bedrock-runtime", config=bedrock_runtime_config()) as client:
        yield client
