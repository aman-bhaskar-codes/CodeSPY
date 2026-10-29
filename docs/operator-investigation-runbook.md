# Investigation operations

Investigations bind a request to an admitted immutable snapshot. Creation, execution,
report publication and model accounting are separate operations. This guide describes
current HTTP contracts and injected worker composition; it does not activate a runtime.

## Authority and prerequisites

Use authenticated same-origin requests with the session cookie, current repository
write permission, `Origin`, `X-CSRF-Token` and an `Idempotency-Key` for each lifecycle
command. A matching replay returns the stored command result; conflicting replay
is rejected. Start, resume and retry retain the original requesting principal.
Another authorized writer creates a separate investigation. Current writers can
request cancellation.

The API runtime uses the restricted API role; worker/finalization uses the broker
role. Operator funding and usage reconciliation require separate authority. Shared
SQL service logins do not independently authenticate end users. Never give the
application migration-owner credentials to avoid a denied operation.

Required components are a retained source snapshot, configured source reader,
model runtime, funded budget, installed price version, authorized disclosure policy
and registered transport. Model availability is metadata, not a live provider test.
A missing runtime/adapter returns `inference_unavailable` before execution starts.

## Create and execute

The route has no `/v2` suffix. Supplying `snapshot_id` selects the durable request
contract; the older request-driven contract remains a compatibility path.

```http
POST /api/v1/repositories/{repository_id}/investigations
Content-Type: application/json
Idempotency-Key: create-example-001
X-CSRF-Token: {session_csrf}
Origin: {application_origin}

{
  "repository_id": "10000000-0000-4000-8000-000000000001",
  "snapshot_id": "10000000-0000-4000-8000-000000000002",
  "target_type": "repository",
  "query": "Inspect the source checks used by request handlers",
  "mode": "short_path",
  "depth": "standard",
  "max_steps": 10,
  "budget_limit_micros": 5000000
}
```

Replace the example identities with authorized retained resources. Creation returns
201 and an `InvestigationSummaryV2` with pending state. It does not start inference.
Modes are `short_path` and `two_specialist`; depth is `quick`, `standard` or `deep`.
Step count is 1–25. A finding target also requires its `finding_id` and persisted
`finding_run_id`; optional `chunk_set_id` binds a stored retrieval selection.

```http
POST /api/v1/repositories/{repository_id}/investigations/{investigation_id}/execute
Idempotency-Key: execute-example-001
X-CSRF-Token: {session_csrf}
Origin: {application_origin}
```

No JSON request body is required. Durable execution returns 202 with
`investigation_id`, `job_id`, `status`, `status_url` and `events_url`. Follow those
references; investigation status and queue-job state have different vocabularies.
The compatibility request-driven path can return 200 and does not establish durable
worker qualification.

## Status, history and streaming

- `GET /api/v1/repositories/{repository_id}/investigations?cursor={cursor}&limit=20`
  returns a bounded history page and its next cursor.
- `GET /api/v1/repositories/{repository_id}/investigations/{investigation_id}`
  returns the scoped detail projection, including available actions and current
  source-support availability for durable results.
- `GET /api/v1/repositories/{repository_id}/investigations/{investigation_id}/events?after_sequence=0&limit=100`
  returns durable sequence-paginated JSON. Continue with the returned `next_cursor`.
- `GET /api/v1/repositories/{repository_id}/investigations/{investigation_id}/stream?after_step=0`
  is the separate live SSE compatibility endpoint. It is not a `/events/v2` route
  and does not accept a durable sequence cursor.

Use durable pages for replay and gap recovery. The workbench shows one saved event
page and up to 200 recent live events, with explicit coverage text. Persisted source
correspondence describes publication-time checks, not proof of a security conclusion.

## Cancel, resume and retry

All commands below use the same session, Origin, CSRF and idempotency headers.

| Command                              | Body                                                                                            | Result and boundary                                                                                                             |
| ------------------------------------ | ----------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `POST .../{investigation_id}/cancel` | None                                                                                            | 200 cancel receipt. Request cancellation and then reread durable status; an in-flight provider exchange may still finish.       |
| `POST .../{investigation_id}/resume` | `expected_failed_job_id` and `expected_spec_hash` from current detail                           | 202 start receipt. Requires awaiting-human-decision state and exact stored job/spec identity.                                   |
| `POST .../{investigation_id}/retry`  | Optional `max_steps` and `budget_limit_micros`; `{}` retains defaults from the original request | 201 new investigation with retry lineage. It is not an in-place reset of the old audit history and must be explicitly executed. |

Cancellation is not an immediate refund or proof that a network request stopped.
Known usage settles; ambiguous dispatch retains an uncertain hold. Never release a
possibly spent reservation merely because the job is cancelled or its lease expired.
See [accounting operations](operator-budget-runbook.md).

## Worker composition and recovery

The application composition root supplies database factories, queue, object store,
admission processor and optional structural/scan/index operations to
`app.workers.composition.build_worker_runtime`. Passing `model_runtime` connects
investigation execution, scoped tool services, encrypted results and maintenance.
It requires encrypted continuation storage. The factory reads no credential files
and creates no cloud resources.

Use the same source reader, scanner capabilities, disclosure policies and model
registry across API and workers. Enable `propose_report_memory` only with the required
model/source configuration. The opt-in consumer proposes candidates; it cannot
accept memory without current owner/admin review.

Run the returned runtime through its existing lifecycle. It owns and joins its
background tasks on shutdown. Current authority tests use additive revisions through
`0040_investigation_runtime_permissions`; historical tests may pin older revisions.
Database `clock_timestamp()` is the lease authority. A Python clock advance does
not establish PostgreSQL expiry.

Recovery reads durable attempts, checkpoints, dispatch receipts and report drafts.
It must not invent provider outcomes or resend under an ambiguous paid-call identity.
Encrypted execution payloads expire after seven days; retain the owner-managed key
for the required lifetime. Missing or expired mandatory history requires a new
investigation rather than reconstructing a paid call under its old identity.

Embedding dispatch claims must commit before external I/O. Maintenance converts
stale markers to uncertain holds; it does not release funds based on elapsed time.
No external embedding client is activated by this worker composition.

## Diagnostics, cleanup and qualification

Safe offline diagnostics are configuration validation and the existing synthetic
unit tests; see [model configuration](operator-model-runbook.md). Do not probe a
provider to investigate an unavailable UI state. For offline indexing checks,
`TIKTOKEN_CACHE_DIR` must identify the verified pinned tokenizer vocabulary.

Stop only owned processes and use the corresponding ownership-manifest cleanup
harness for disposable containers. Preserve ownership records if cleanup fails.
Never remove developer volumes, keys or unrelated containers.

[Investigation evidence](acceptance/investigations.md),
[reviewed-memory evidence](acceptance/reviewed-memory-context.md) and
[workspace evidence](acceptance/frontend-workspace.md) distinguish real PostgreSQL
and isolated-reader checks from synthetic provider responses. No new tests were run
for this documentation reconciliation. Live quality, the complete single
upload-to-saved-result browser journey and hosted deployment remain unqualified.
MCP investigation inspection is read-only; see its [runbook](operator-mcp-runbook.md).
