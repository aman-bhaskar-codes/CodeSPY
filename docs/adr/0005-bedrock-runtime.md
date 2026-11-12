# ADR 0005: Bedrock runtime boundary

Status: accepted for generation and query embeddings; dense index publication is deferred. The
[acceptance record](../acceptance/bedrock-runtime.md) owns verification evidence and limits.

## Decisions

1. Library code never creates AWS sessions or discovers credentials. The owner builds an
   `aioboto3` session; `adapters/model/bedrock_client.py` only turns it into a `bedrock-runtime`
   client pinned to `ap-south-1`, one attempt, bounded timeouts and connections, no endpoint
   override. `core/bedrock_runtime.py` assembles the client, the accounted embedding gate and the
   query embedder, and refuses to build unless execution is `live` with explicit egress approval.
2. Generation keeps the existing Converse codec, transport and gateway. Native Bedrock schema and
   streaming stay refused.
3. Titan Text Embeddings V2 is an async adapter over an injected client. A missing client is an
   error: there is no simulation inside the provider adapter (the fake adapter owns simulation).
   Requests the adapter could never send are refused before any budget exists.
4. Every external embedding call is disclosure-checked (provider and region), reserved, claimed
   (committed before I/O), sent once and settled. A failure or timeout after the claim is an
   uncertain hold that only an operator or the accounting recovery pass resolves.
5. Query embeddings are redacted with the same secret transform used for chunks, and each query is
   its own billed operation. Dense search refuses a raw external-provider port: only the accounted
   query embedder may serve a non-`fake` provider.
6. Unknown repositories resolve to the default disclosure policy, which refuses every external
   provider. The contract default region (`us-east-1`) is unchanged; the owner configures an explicit
   `ap-south-1` policy per repository.
7. Live checks are owner-run (`scripts/bedrock_smoke.py`, `live_bedrock` marker). CI stays offline
   and strips `AWS_*`.

## Deferred

Dense index publication (embedding a sealed chunk set and publishing it). The database refuses it
by design; see the acceptance record for the exact guards and why this needs its own design.
