# Reviewed repository memory and bounded context

Repository memory uses PostgreSQL as its authority. Writers submit candidates;
only an organization owner or administrator with current repository write access
can accept, reject, restore or supersede them. Accepted records are reusable
knowledge, not proof of their statements. Notes without source pointers never
establish grounded findings.

## Storage and authority

- Additive revision `0032_reviewed_memory` retains memory identities, immutable
  versions, review events and principal-bound request receipts. Existing records
  need review; migration does not fabricate approval or authorship.
- Candidates expire after seven days. Acceptance and explicit restoration grant
  ninety days. Reads do not extend retention. Archive and expiry exclude records
  from new selection immediately; payload cleanup follows a seven-day grace.
- The worker role cannot review memories. A restricted database function purges
  eligible payloads while retaining identifiers, review metadata and digests.
  The existing worker loop schedules the bounded maintenance pass. Older schemas
  without this function return no work.
- Source cleanup does not erase audit identities. Conversely, memory retention
  does not retain uploaded source indefinitely. Unavailable sources exclude memory
  from retrieval; management views hide its text.

## Selection and composition

Memory retrieval requires a target snapshot, scans at most 100 candidates and
returns at most ten entries, including at most two historical hints. It uses
PostgreSQL text search and current authorization, expiry and source checks.
Historical paths and file hashes can establish source correspondence, not that
an old conclusion applies to new code. Changed files remain explicitly historical.

The provider composer measures the prepared wire body, including system messages,
tool definitions and required conversation/protocol history. It accounts for
output reserve, profile bounds and a safety allowance. Memory is untrusted data,
limited to a quarter of optional token capacity, at most 1,024 estimated tokens
and 8 KiB. Compaction never rewrites native continuation items or tool/result pairs.
Mandatory overflow stops before provider dispatch; no summarization call is made.

Logical calls retain a reference-only manifest with stable selection and wire
digests. Replay preserves selected versions and original omission decisions.
Dispatch rechecks current authority and retained target/reference sources in its
transaction. Withdrawal before dispatch prevents sending and returns the unused
reservation through the existing accounting authority. A previously sent call
cannot be recalled or made free by archiving memory.

## Interfaces and rollout

- Existing memory creation submits a candidate. Review, bounded listing and history
  endpoints extend the existing API; frontend transport types are generated.
- The workbench exposes review reasons, expected versions, expiry, source identity,
  historical classification and selection omissions. Context preview is explicitly
  a selection preview, not a provider-qualified request.
- MCP remains read-only and requires snapshot scope for memory queries.
- Configured model runtime accepts `MemoryContextSettingsV1`; default mode is
  `disabled`. `shadow` excludes memory from provider input; `reviewed` selects
  accepted records. Evidence services are injected through runtime/gateway wiring.

## Verification limits

Real PostgreSQL tests cover command replay, role isolation, review, expiry,
withdrawal, frozen context and payload retention. Connected HTTP tests use the
real isolated Linux source reader with fixture-owned archives, including changed
and identical-file historical snapshots. Gateway integration uses a synthetic
HTTP provider with real accounting; it makes no live provider request.

The frozen sixty-case corpus has twenty development and forty held-out cases.
It measures mechanical retrieval, trust filtering and context packing. Its notes
are synthetic human-note fixtures, not independently established code facts.
It does not establish answer-quality improvements or official LongMemEval scores.

Durable worker composition connects the configured gateway, scoped encrypted model
results, node effects, source-support checks and atomic report publication. Tool
results and provider continuation references survive checkpoint recovery. Report
observations become candidates through an opt-in outbox consumer; they never
become accepted memory without human review.

Connected tests exercise both investigation modes using real PostgreSQL, an
isolated Linux source reader and synthetic OpenAI-shaped HTTP responses. They
interrupt execution after a committed tool result and after the draft checkpoint,
then verify retained-result reuse, a single report and one review candidate.
These tests do not qualify live answer quality or the complete browser journey
from upload through saved investigation. Reviewed model memory remains disabled
by default. Public hosting and real provider activation remain owner-managed.

The current regression evidence is recorded in the investigation and accounting
acceptance documents. Historical migration tests run at explicitly declared schema
boundaries; current authority tests use the latest additive revisions. No
unrestricted backend-suite or live-provider success is inferred from selected
suite results.
