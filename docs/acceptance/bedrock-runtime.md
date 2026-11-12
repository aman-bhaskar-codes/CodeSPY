# Bedrock runtime

Amazon Bedrock generation (Converse) and Titan embeddings are wired behind explicit owner-built
clients, with disclosure checks and budget accounting. No live Bedrock call has been made by the
test suites; live behaviour is verified only by the owner-run smoke script.

## Implemented boundary

- **Client.** `open_bedrock_runtime(session)` yields an `aioboto3` `bedrock-runtime` client for
  `ap-south-1` with `total_max_attempts=1`, 5 s connect and 60 s read timeouts and 4 connections.
  `ConfiguredAsyncConverseClient` and the Titan adapter re-verify region and attempts.
- **Composition.** `build_bedrock_wiring(settings, session=..., reserve_sessions=...,
  dispatch_sessions=...)` returns the validated Converse client (for
  `build_model_runtime(bedrock_client=...)`), an `AccountedEmbeddingGate` and an
  `AccountedQueryEmbedder`. Offline settings are refused. `ApplicationPorts.query_embedder` carries
  the embedder to the dense and hybrid search endpoints.
- **Titan V2.** Async `invoke_model` with a total timeout, local input bound (200,000 bytes),
  response cap (256 KiB), dimension 256/512/1024, finite non-zero vectors, sanitized errors and
  `possibly_dispatched` on every failure. Provider id `aws_bedrock`.
- **Accounting.** `AccountedEmbeddingGate`: provider and region must be permitted by the repository
  policy; the adapter's `validate` runs before any reservation; the reservation and the dispatch
  claim commit before the call; success settles the actual token charge; failure or timeout after the
  claim becomes an uncertain hold and the same operation and text can never be sent again. Reservation
  and dispatch use separate session factories because the database roles differ.
- **Queries.** `AccountedQueryEmbedder` redacts the query, applies the repository policy
  (default refuses) and treats each query as a new operation. `DenseSearchService` raises for a
  non-`fake` provider served by a raw port.

## Limitations

- **Dense index publication is wired through the job system but not composed into any runtime.** The
  database layer, embed operation, job service, consumer and HTTP routes exist
  ([Dense index publication](dense-index-publication.md)); no shipped entry point builds the Bedrock
  client, `embed_config` or the worker's embed operation. Before that work, migration 0023 refused
  it at the database:
  `ck_index_qualified` allows only lexical indexes on a chunk set; the
  `codespy_index_result_guard` trigger rejects any `chunk_embeddings` row whose chunk or index
  belongs to a chunk set ("qualified embedding publication unavailable"); jobs and receipts exist
  only for the lexical `index_snapshot` operation. Embedding a sealed chunk set needs its own
  fenced job operation, spec, receipt, guards and role grants, designed and migrated deliberately.
  Until then the query embedder has nothing to search, and the unqualified legacy path still refuses
  external providers. No guard was weakened.
- The conservative token estimate (one per three UTF-8 bytes) is an upper bound used only for the
  reservation ceiling; settlement uses the provider's reported count.
- A settlement that fails after a successful call logs a count-free error and leaves the claim for
  recovery to hold; the vector is still returned.
- Model ids in the default catalog (India inference profile) need the owner's confirmation of
  availability and processing regions before any live use. A Mumbai client does not prove where an
  inference profile processes data.
- Streaming, native structured output, Knowledge Bases, BYOK and fallback routing are not provided.
- The local runner (`scripts/phase2_local.py`) does not enable Bedrock; wiring it is meaningful only
  once dense indexes can be built.

## Verification

- Client builder, Titan adapter (11 cases), composition and policy resolution run offline with
  synthetic clients and static dummy keys; no network.
- The accounted gate runs against real PostgreSQL accounting routines: success settles the actual
  charge and replay is refused; policy and region denial reserve nothing; unsendable requests and
  over-budget estimates never reach the provider; a failure after the claim leaves an uncertain hold.
- Query redaction, per-query operation keys, empty-after-redaction refusal and raw-port refusal.
- Regression: `phase5`, `model_connections`, vector storage; `ruff` and `mypy` with the CI config.

## Owner-run live check

```
CODESPY_LIVE_BEDROCK=1 AWS_PROFILE=<profile> uv run --project backend \
  python scripts/bedrock_smoke.py --converse-model <model-or-inference-profile-id>
```

Two small paid calls to `ap-south-1`. It prints ids, token counts, latency and vector size only.
