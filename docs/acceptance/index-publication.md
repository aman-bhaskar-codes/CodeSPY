# Atomic index publication

Indexing can run through the existing queue consumer when an `IndexOperation`
is explicitly injected. Unconfigured workers defer valid indexing deliveries
without claiming them. The operation loads the immutable attempt specification,
checks current write authority and retained input, then releases database locks
before reading source through the isolated reader. File size and SHA-256 are
checked before staging, and authority is checked again before building.

One preparation deadline covers source reads, staging, runtime execution and
receipt validation: at most 180 seconds, with 65 seconds reserved from the live
database job deadline. Raw staging uses private directories, fixed UUID filenames
and durable ownership records. Normal completion and cancellation join bounded
cleanup. Exclusive startup recovery removes registered crash debris before queue
consumption. Recovery must not sweep an active indexing operation's resources.

The finalizer accepts only a trusted preparation receipt. The existing
`PublicationExecutor` starts its aggregate 60-second budget before validation,
serialization and lock acquisition, continuing through awaited commit. Monotonic
checkpoints prevent publication after bounded synchronous validation overruns.
Statement, lock and idle timeouts remain subordinate controls. Live database
lease/deadline authority is checked again before terminal publication; sufficient
remaining lease and deadline include the five-second safety allowance.

One guarded transaction publishes the immutable chunk set, safe content,
provenance, weighted lexical vectors, ready/partial index, attempt receipt,
terminal job/attempt states and event. Path terms have weight B and safe content
has weight C, matching the existing lexical profile. No raw source bytes are
stored in qualified chunks. Concurrent identical publications reuse one matching
set with separate job receipts. Conflicting replay rejects without changing the
prior set. Published IDs derive from persisted file and materialization identity;
callers cannot replace them with candidate IDs.

An uncertain commit acknowledgement is resolved by reading the exact durable
job/attempt/fence receipt through a new session. An unavailable recovery read
reports an unknown outcome; it does not blindly create another set. Cancellation
and timeout roll back and recover the connection. Index source/spec/output
integrity failures are permanent. Runtime, process, timeout and cleanup failures
use existing bounded retry and recovery behavior.

## Observed qualification

On 24 October 2026, reference
`83767b105f0eb0870c79f84402241d729084120b` plus local uncommitted changes:

- **983 backend tests passed, 73 skipped** in 28.88 seconds. PostgreSQL and native
  fixtures are opt-in in that command and ran separately below.
- **77 PostgreSQL integration tests passed, one skipped** in 93.74 seconds.
  These cover finalizer replay, concurrent identical requests, authority loss,
  actual database lease expiry, read-time cancellation/revocation/source deletion,
  multi-statement aggregate timeout, cancelled transaction recovery, lost commit
  acknowledgement, prior-set preservation, lifecycle/migration guards and older
  ingestion, scan and structural operations. Controlled preparation/readiness
  fixtures are labelled synthetic; they do not qualify native isolation.
- The same run includes a **real admission → isolated source reader → Linux
  indexer → PostgreSQL publication** journey using owned synthetic input.
- The skipped maximum-size publication test ran separately: **one passed**.
  A real Linux preparation over 128 admitted files totaling 16 MiB produced 4,096
  chunks with explicit partial coverage; atomic publication took **6.660 seconds**
  under the unchanged 60-second budget. That fixture stages verified input
  directly for capacity measurement; it does not measure 128 sequential isolated
  source-reader calls or qualify every possible maximum-size draft.
- **Eight real Linux runtime tests passed** in 29.01 seconds. Maximum input
  preparation took 12.902 seconds. Sampled maximum memory was 121,844,531 bytes,
  not a true peak; configured/inspected limits remain 1 GiB and 180 seconds.
- Strict typing passed for **224 application files**. Ruff passed for application
  code and touched tests. No frontend or public schemas changed; frontend checks
  were not rerun.

Qualified local indexer image:
`sha256:b696a10108e773f47a22f60ceab232b2f86677a17989731d00eb0f933d1e8bdb`

Runtime source fingerprint:
`3d91ce01d725e3ee3f538e6582736207386c5e1e3f5db108ed4b56e430c2d5f2`

The image was rebuilt offline from existing hash-verified locked wheels. Native
source reading reused image
`sha256:17d883d732e475f175974247671d59f47a55fbf3e151c2b6185bf095f244fc31`.
No dependencies, historical migrations, API keys or provider configuration were
changed. No AWS, model/provider, deployment or GitHub calls were made.

## Remaining integration

Qualified-set selection for search/context/memory is covered by
[qualified index reads](index-reads.md). Public indexing routes, workbench
controls and read-only MCP status remain pending. Default activation
stays disabled. Maximum canonical-draft/provenance edge cases and the complete
search-to-context/UI journey require further qualification before public
activation. This evidence does not establish a deployed or complete MVP.

Owned PostgreSQL/emulator containers and network were removed after verification.
Final test-cluster, indexer and source-reader container inventories were empty;
trusted images remain idle. Developer volumes were preserved.
