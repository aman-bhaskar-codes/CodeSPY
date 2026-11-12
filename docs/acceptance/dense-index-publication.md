# Dense index publication

A sealed lexical chunk set can be embedded through accounted provider calls and published as an
immutable dense index by a fenced `embed_chunk_set` attempt. The database refuses every other
writer. The job is creatable over HTTP, claimed and recovered by the job service, consumed by the
worker and visible through a status route; the layers below are verified against PostgreSQL.

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

## Job plumbing

- **Creation.** `POST .../snapshots/{id}/indexes/{chunk_set_id}/dense` takes only an idempotency key:
  the provider, model, dimension and price version are server configuration
  (`ApplicationPorts.embed_config`), and the repository's disclosure policy
  (`ApplicationPorts.embed_policy`) must permit the provider and `ap-south-1` (403
  `disclosure_denied` otherwise, 503 `embed_unavailable` when either is unconfigured). The chunk set
  must be sealed with a lexical receipt and the price version must be active. A repeated key replays;
  a changed request conflicts; an already published profile conflicts. Creating a job reuses the
  immutable spec for the same profile.
- **Status and cancel.** `GET .../embed-jobs/{job_id}` returns the job, the receipt once published
  and `can_cancel`; `POST .../embed-jobs/{job_id}/cancel` cancels. The generic job routes answer 409
  `embed_operation_unavailable` for this operation, as for the other typed operations.
- **Job service.** Enqueue into the `embed_jobs` projection with the V7 envelope; claim inserts the
  attempt spec; transient failures retry with a new fence; recovery covers deadline, lease expiry and
  user cancellation. `embed_*` failure codes are accepted only for this operation.
- **Worker.** The consumer parses the V7 envelope first, checks the spec id against the stored job,
  acks only a durable outcome, defers when no embed operation is configured and maps `EmbedFailure`
  to its code and transient flag. The operation re-checks the disclosure policy before any spend
  (`embed_disclosure_denied`), and without a policy resolver the default policy refuses every
  external provider. `build_worker_runtime(embed=...)` wires it.
- **Search.** The existing dense search route serves the published index through the accounted
  query embedder (`ApplicationPorts.query_embedder`).

## Limitations

- **Recovery of a revoked writer is broken at head for every operation.** Since revision 0040,
  `lock_scope(access=False)` still runs the actor-write check inside `codespy_lock_scoped_snapshot`,
  so `recover_due` raises for a job whose principal lost write access. The equivalent
  `index_snapshot` test fails the same way on `main`; the embed test is a strict expected failure
  (`test_revoked_writer_is_terminalized_by_recovery`) that flips when the defect is fixed.
- There is no retry route for embed jobs; creating a request with a new key reuses the spec, resumes
  from staged vectors and never pays twice for a settled chunk.
- A held chunk needs an operator to resolve its uncertain hold before the job can finish; there is no
  automatic resolution.
- The local runner (`scripts/phase2_local.py`) does not enable Bedrock, so no deployment composes the
  embed operation, the query embedder or `embed_config` yet.
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
- job plumbing: contracts and projection; enqueue idempotency and conflicts; claim binding and retry
  with a new fence; failure-code scoping; running cancellation recovery; creation refusals (no sealed
  set, no price, unsupported dimension, forbidden provider or region); status and cancel; the consumer
  end to end (durable receipt before ack, duplicate wakeup, unconfigured defer, forged spec id,
  transient failure then hold); HTTP create, replay, CSRF, mismatch, status, cancel, 503, 403 and 404;
  and a full HTTP create, worker run, status with receipt and dense search hit
- regression: the retrieval, job, accounting, memory, investigation and AST-facts suites show the
  same failures with and without these changes
