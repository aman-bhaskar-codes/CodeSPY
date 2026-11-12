# Dense index publication

A sealed lexical chunk set can be embedded through accounted provider calls and published as an
immutable dense index by a fenced `embed_chunk_set` attempt. The database refuses every other
writer. The job's creation, queue consumer, recovery and HTTP route are not wired yet (see
Limitations); the layers below are verified directly against PostgreSQL.

## Implemented boundary

- **Schema (revision 0044).** `embed_specs` (immutable; one chunk set, one profile, one price version),
  `embed_attempt_specs`, `embed_publication_receipts`, `embed_vector_stage` and the new `jobs`
  operation `embed_chunk_set` with `embed_spec_id`. `ck_index_qualified` now admits a sealed dense row
  whose id is `uuid_generate_v5(chunk_set_id, 'dense-v1:<provider>:<model>:<dimension>')` for
  dimensions 256/512/1024. One lexical index per set and one dense index per profile; several dense
  profiles may coexist. Downgrade is refused while dense history exists.
- **Guards (revision 0045).** Dense `search_indices` and `chunk_embeddings` rows may be inserted only
  by a live, fenced embed attempt bound to its spec, over a chunk set that already has a lexical
  receipt, and each published vector must equal the vector staged for that chunk. Updates and deletes
  of published rows stay rejected, the seal is per index (a receipt freezes it), and deferred checks
  require the receipt, an exact chunk count, the right state for the set's coverage and the embedding
  digest at commit. Lexical publication guards are unchanged.
- **Accounting (revision 0046).** The worker role cannot call the embedding reserve routine, and the
  existing claim and settle routines are not bound to a job. `codespy_embed_reserve`, `_claim` and
  `_settle` are `SECURITY DEFINER`, require the live embed attempt, force the job's principal, derive
  the idempotency key from (spec, chunk) and record every reservation in the immutable
  `embed_spend_ledger`. A staged vector needs its chunk's ledgered, dispatch-claimed reservation;
  a receipt needs every ledgered reservation settled and its ledger digest and token total to match.
- **Operation.** `EmbedOperation` (`services/embed_job.py`) does, per chunk and under bounded
  concurrency: reserve, claim (committed before any provider call), embed, stage the vector durably,
  settle. A crash between staging and settlement resumes from the staged vector with no second call
  and no second charge; a failure or timeout after the claim is an uncertain hold that is never
  resent automatically. `EmbedFinalizer` then publishes the sealed index, embeddings, receipt and
  terminal job state in one fenced transaction and removes the staged vectors.

## Limitations

- **Not yet wired:** creating an embed job (spec insert plus enqueue), `JobService` claim for the new
  operation (it must insert `embed_attempt_specs`), the outbox envelope and consumer dispatch with
  `embed_*` failure codes, recovery and cancel/retry branches, the HTTP route, and the worker
  composition. Until then an attempt exists only in tests.
- A held chunk needs an operator to resolve its uncertain hold before the job can finish; there is no
  automatic resolution.
- Query-time dense search through the HTTP API needs a configured query embedder and a published
  dense index; neither is enabled in the local runner.
- The 60-second finalization budget has been exercised on small sets only. A 4,096-chunk set (about
  16 MiB of JSON vectors at 1024 dimensions) has not been measured.
- The HNSW index covers 1024 dimensions only; other dimensions use an exact scan.
- No live provider was called; the provider is a synthetic client.

## Verification

Owned disposable PostgreSQL 16 at schema head, with the real roles:

- schema: tables, constraints, uniqueness, round-trip downgrade, downgrade refused with history
- guards: a full fenced publication commits; every deviation is rejected atomically with its own
  reason (no worker context, unclaimed reservation, vector differing from the staged one, wrong
  dimension, wrong deterministic id, wrong digest, missing receipt at commit); published rows are
  immutable; another operation's attempt and the API role cannot publish
- accounting: scope, replay, changed-request conflict, one claim winner, uncertain hold, ledger
  immutability, staged vectors bound to the ledger
- operation: end-to-end publication with pgvector nearest-neighbour search, crash-and-resume without
  re-calling or re-paying, hold after a failed provider call, stale fence refused
- regression: the lexical publication, memory and AST-facts storage suites show the same failures as
  before these revisions (seven pre-existing at head)
