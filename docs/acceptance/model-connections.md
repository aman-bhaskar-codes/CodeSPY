# Configured model connections

> **Status reconciliation — 28 October 2026:** Later [investigation](investigations.md) and [accounting](model-accounting.md) records cover worker composition and shared embedding authority/recovery. The transport results below remain synthetic and scoped to their original schema/environment; they do not qualify live providers or the complete product journey. No tests were rerun for this documentation update.

## Implemented behavior

Five independently configured slots route to direct OpenAI Responses, asynchronous
Bedrock Converse and direct Gemini generateContent. The registry admits only
registered, enabled, locally qualified, capability-compatible and policy-permitted
profiles with an active exact price version. Processing regions must be explicit;
there is no fallback to a fake provider when configuration is missing.

The configured gateway binds the requesting principal, wire/profile digest,
continuation, scope and captured price into request identity. It commits one dispatch
claim immediately before the network attempt. Replay, concurrent callers and an
ambiguous committed attempt cannot emit a second call. HTTP/SDK retries are off.
Known usage is charged before output validation; missing usage and unpriced thinking
or cache categories retain conservative holds. Numeric receipts and charges commit
atomically. Normal settlement, conflicting replay and debt accounting use repaired
additive PostgreSQL routines; historical migrations remain unchanged.

Opaque reasoning/tool items survive encrypted, principal/investigation/profile-bound
storage. Long or missing native tool IDs receive reversible internal mappings via
that retained state. Runtime roles cannot directly read ciphertext or mutate receipt
history. Retention deletes at most 100 expired records per maintenance pass. Scoped
read checks and database-time expiry prevent stale references from being reused.

The application injects the configured gateway through a typed port. Existing
investigator tools retain their schemas and are validated before local execution.
Checkpoint operations use short authorized fenced transactions. An independent
connection can acquire the investigation row during a model call; expired database
leases reject later publication. Stable logical call IDs prevent a resumed request
from producing a second paid dispatch. Existing distributed job scheduling and
full crash/resume semantics are not established by this transport integration.

Repository-scoped model availability exposes sanitized configuration/qualification
status. OpenAPI and TypeScript definitions are regenerated. Disabled profiles cause
no credential discovery or client construction. The owner supplies secrets, prices,
funding, model IDs, processing-region policy and live approval separately.

## Observed verification

Baseline HEAD: `66109e610ac5d9ff7aac99143bf28aed558177b6`, with preserved prior
uncommitted work. Results apply to the local working tree, not a published commit.

| Check                                                              | Observed result                                            |
| ------------------------------------------------------------------ | ---------------------------------------------------------- |
| Affected backend regression                                        | 1,117 passed in 31.74 seconds                              |
| Owned PostgreSQL accounting/model/checkpoint integration           | 50 passed in 63.90 seconds                                 |
| Strict application typing                                          | Passed, 253 files                                          |
| Ruff on application, new model tests and additive model migrations | Passed                                                     |
| OpenAPI export and frontend generation                             | Passed                                                     |
| Frontend tests                                                     | 32 passed, 10 files                                        |
| Frontend typing, lint and production build                         | Passed                                                     |
| Exact public lockfile dependency audit                             | No known vulnerabilities found                             |
| Whitespace validation                                              | Passed                                                     |
| Owned PostgreSQL cleanup                                           | Container and network removed; developer volumes untouched |

Backend commands used the project's Python 3.12 disposable runtime and an explicitly
mirrored source tree; the final authored Python sources were synchronized before
execution. Broad collection in the original checkout was interrupted while waiting
on filesystem traversal; that attempt is not counted as passing. The affected run
covers model/unit/transport tests, accounting unit tests, investigator and retrieval
regressions, and existing parser/admission/foundation unit checks. It is not an
unrestricted whole-repository database/container/browser qualification.

PostgreSQL checks used only the existing owned disposable harness and schema target
`0030_model_runtime_authority`, including separate API/broker roles. Coverage includes
five slot accounting exchanges, current scope denial, exact monetary settlement,
request replay/concurrency, unknown holds, invalid-output charging, encrypted state,
retention, cancellation, API execution/reload, sanitized unavailable errors, short
checkpoint transactions and actual database-time stale-worker rejection.

Tests use HTTPX MockTransport or an injected synthetic async Converse client. They
are not live provider qualifications. The installed disposable runtime differs from
the current lockfile: its older filelock/virtualenv versions reported seven
advisories; the separate audit of exact locked public versions reported none. No
new dependency or lockfile changes were made for this work.

Reproduced failures before fixes included incorrect SQL settlement arithmetic,
optional investigator-tool schema rejection, missing client metadata validation,
live admission without an input-bound qualification, nested output-schema failure,
missing retention integration, unpriced reasoning settled as free, known charges
rolled back by unexpected continuation state, unhandled unavailable-model API errors,
checkpoint locks spanning inference, and raw invalid output copied into debug logs.
Other added assertions provide regression coverage without an independent failing
observation for every assertion.

## Explicit limits and owner activation

- Live provider access, model-specific quality, exact pricing, processing residency,
  native SDK cancellation and real signed tool continuation remain owner-run checks.
- Token streaming is explicitly unavailable. Provider stream parsers remain deferred;
  no raw stream can bypass accounting. Existing UI progress remains non-streaming.
- The UTF-8 envelope estimate is a heuristic, not a proved tokenizer bound. Live
  routing requires a separately owner-qualified input bound. Gemini unbounded
  default thinking is live-ineligible; unsupported numeric OpenAI thinking budgets
  fail preparation. Gemini's no-thinking policy sends an explicit zero budget.
- The pinned Bedrock SDK lacks native Converse schema support; preparation rejects
  it. Bedrock's payload cap applies after SDK decoding; hard wire-byte bounding is
  unqualified.
- Shared embedding role/routine integration remains a separate accounting deployment
  gate. Generation-role tests do not establish its correctness. Existing unsent
  abandoned reservations may need reviewed operator release; unknown dispatches are
  never automatically refunded. There is no external billing reconciliation feed.
- Continuation key rotation requires owner-coordinated draining/retention. This is
  encrypted protocol state, not a new memory service or exposed chain of thought.
- No full live upload-to-investigation browser journey, hosted deployment or
  independent review agent was run. Author review was inline under the approved
  no-agent workflow.

The [operator runbook](../operator-model-runbook.md) describes safe configuration,
composition and the required activation gates. AWS, credentials, provisioning,
deployment and GitHub publication were not performed. The whole application is not
declared production-ready by this increment.
