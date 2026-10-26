# Model connections

CodeSPY can route generation to direct OpenAI Responses, Amazon Bedrock Converse
and direct Gemini generateContent through the same accounting gateway. The five
configuration aliases are `openai_fast`, `openai_reasoning`, `claude_fast`,
`claude_reasoning` and `gemini_general`. Aliases describe routing slots; they do not
assert model quality, account access or regional availability.

## Configuration and application wiring

`config/models.example.json` is a disabled, non-secret example accepted by
`ModelRuntimeSettings.model_validate_json(...)`. Every model ID, revision, price version, processing region
and capability must be qualified by the owner. The example deliberately contains
no active prices, disclosure permissions or live approval. Do not copy placeholder
IDs into an enabled profile. No credentials are loaded by validation.

The composition root opens `build_model_runtime(settings, ...)` for its full
application lifespan and injects the yielded runtime into
`ApplicationPorts(model_runtime=runtime, ...)`. Supply separate admission/API and
accounting/broker session factories. Production database roles must be granted only
the reviewed constrained routines. The application does not fund budgets, create
prices, discover models or silently fall back to a fake provider. The explicit fake
adapter remains available for local fixtures.

The composition root supplies an asynchronous credential callback returning
`SecretStr` only when a call is admitted. CodeSPY does not inspect `.env` files or
configure keys. An HTTPX client can be injected for offline testing. Live HTTP
client creation requires `execution_mode="live"`, `allow_live_egress=true`, enabled
profiles with `owner_approved_live`, `locally_qualified` and
`input_bound_qualified`, and registered transports. Request preparation is offline.
The UTF-8 envelope estimate plus fixed allowance is a local conservative admission
heuristic; it is **not a proved tokenizer bound**. Owners must qualify it against
each exact model/tokenizer and bounded input class before approving the profile.
Unsupported billing categories remain held for reconciliation.

For Bedrock, the owner creates and closes an asynchronous SDK client with region
`ap-south-1` and `Config(retries={"total_max_attempts": 1, "mode": "standard"})`,
then wraps it in `ConfiguredAsyncConverseClient` and injects it as `bedrock_client`.
The wrapper verifies metadata without creating an AWS session. A Mumbai client does
not prove the processing regions of an inference profile: list and authorize every
destination separately. The pinned SDK lacks native Converse schema support, so
that capability is rejected. The SDK response cap is checked after decoding; a hard
wire-byte cap is not qualified.

Each enabled profile needs a separately installed immutable price version and an
explicitly funded repository budget. Active-price configuration is an allowlist,
not funding or a substitute for the database price. Configure repository disclosure
permissions by organization/repository; unknown regions fail admission. Gemini
provider-default thinking is ineligible for live routing without a bounded policy.
Profiles using tools or reasoning require an injected 32-byte continuation cipher
key supplied by owner-controlled secret management, never stored in this repository.

`GET /api/v1/repositories/{repository_id}/models` returns scoped sanitized
availability. `locally_qualified` means local protocol qualification;
`live_access_verified` remains false. Availability does not call providers.

## Dispatch, accounting and recovery

Calls reserve against the captured price and bind request, actor, profile and
configuration into request identity. The dispatch marker commits immediately before
the network operation. Automatic SDK/HTTP retries and provider fallbacks are off.
Repeated committed requests do not dispatch again; completed replay currently
returns a stable already-completed error rather than caching generated text.
Known usage settles even when output is refused or fails local validation. Missing
or inconsistent usage, cancellation and ambiguous outcomes keep funds held; they
must not be treated as zero-cost or automatically refunded.

Generation mutations use constrained PostgreSQL routines. API/broker direct DML on
financial tables is revoked. The SQL login is a trusted service identity, not an
independent per-end-user authentication boundary; servers still authorize actors.
Existing internal audited reconciliation remains the operator path for uncertain
calls. No external reconciliation endpoint or automatic inference from elapsed time
is added. Shared embedding accounting is separate from generation transport code.

Inject `runtime.maintenance_tick` into the existing worker's
`WorkerRuntime.run(..., model_maintenance=runtime.maintenance_tick)`. It conservatively
holds abandoned dispatched calls after 300 seconds and removes at most 100 expired
continuations per pass, using database time. The worker owns cancellation of its
periodic task. No background task starts merely by importing or validating config.
A process dying while a reservation is still unsent/reserved may require operator
release; this maintenance pass never guesses that an ambiguous dispatch was free.

Opaque protocol state is AES-GCM encrypted with tenant, repository, principal,
investigation, continuation ID and profile binding. It is immutable, expires within
seven days, is denied on scope/permission mismatch, and cannot be read as raw table
data by runtime roles. The owner must retain the encryption key for that lifetime.
Key rotation needs an owner-coordinated retention/drain procedure; automatic key-ring
rotation is not implemented. Continuations are not agent memory or user-visible
reasoning. Numeric usage receipts contain no prompts, headers or signed reasoning.
Database rollback refuses to erase populated transport history.

## Limits and qualification

HTTP calls have two active slots and eight waiting slots per transport instance,
a total deadline of at most 120 seconds, a 1 MiB request cap, a 2 MiB decoded
response cap and bounded tool arguments. Limits are process-local, not distributed
rate quotas. Thinking/output exposure is reserved conservatively; cache writes and
unqualified modality charges remain unknown. Tools are declarations only: existing
scoped investigator routing validates and executes local tools.

Token streaming is unavailable. The UI may display existing investigation progress
while non-streaming calls complete. No raw streaming transport bypasses accounting.
Provider-specific streaming parsers remain deferred behind this explicit gate.

Local tests use HTTPX MockTransport or an injected synthetic async Converse client
and owned PostgreSQL. They establish no paid model access, live quality, real
pricing, provider residency, real Bedrock transport cancellation or hard wire limits.
Investigation checkpoint operations use short authorized transactions with database-time
lease checks; the HTTP handler holds no checkpoint transaction during inference.
Distributed investigation job scheduling and live crash/resume quality remain separate
qualification work. Shared embedding mutation routines/role wiring inherited from the
accounting dependency are not qualified by generation transport tests; deployment
requires that separate role integration to pass.

Before live activation, the owner must verify exact model IDs and API parameters,
account permissions, pricing/counter interpretation, input bounds, processing
regions, timeout/cancellation behavior, continuation signatures, tool roundtrips,
output quality, budget recovery and deployment role grants with a small explicit
spend cap. Credentials, AWS setup and deployment are owner responsibilities.
