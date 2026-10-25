# Durable indexing requests and recovery

Indexing uses the existing PostgreSQL jobs, attempts, outbox and recovery owner.
Creation commits the immutable admitted-input specification, job, wakeup and
queued event together. Concurrent identical request keys return one job; keys
cannot cross principals, configurations, snapshots or retry parents. Readiness
runs before writes and validates the configured pinned runtime. Input selection
comes from retained sealed inventory, never a client-supplied file list.

Selection is deterministic and bounded by 128 files, 16 MiB total, 2 MiB per
file and the existing specification byte bound. Sensitive, ineligible or over-limit
files have explicit omission counts. A conservative 16 KiB header allowance can
reduce packing for long paths; metadata omissions are reported as manifest limits.

Claims use existing database capacity, deadlines, leases, fences and backoff.
Each attempt gets an immutable specification binding and no analysis run.
Automatic retries retain the job/specification. Manual retries require the
original principal and current write permission, retain the verified immutable
input, and create a new job/deadline with a parent reference. Unsupported permanent
failures are rejected. An expired original deadline requires a new explicit
request. Historical jobs and sealed snapshots are preserved.

Recovery terminalizes queued expiry without a dispatcher, closes requested
cancellation and detects lost write authority. Expired leases are replaced with
new attempts and higher fences. Repeated terminal recovery does not change source
objects, submissions or upload accounting. Indexing never reserves upload bytes.

The new queue format is validated against its durable job/specification. When no
index operation is injected, live deliveries are deferred without claiming;
terminal, obsolete or mismatched deliveries are acknowledged appropriately.
Existing queue formats retain their contracts. Legacy HTTP job routes reject
indexing records rather than reinterpret them. No indexing API route is activated.

## Runtime compatibility correction

A discovered mismatch allowed 96 KiB specifications but only 66 KB runtime
requests. The reader now accepts 98,368 bytes, covering the existing 98,304-byte
specification plus its fixed wrapper. Readiness requires explicit advertisement
of this bound, rejecting older probes. Source/chunk/output/memory/time limits are
unchanged. The corrected image was rebuilt offline using hash-verified locked
wheels from the prior owned image.

Image qualified in the request-lifecycle run:
`sha256:ab253332b33fd1f9b3cb6ad96b12191222d8e5ef3933f3448dc2138adf832d75`

Runtime source fingerprint:
`3d2078fc93d3f9d25c61d8855e1dc62a5b36283c24797a9e22d24b24b65a98a7`

## Observed qualification

On 24 October 2026, reference
`83767b105f0eb0870c79f84402241d729084120b` plus local uncommitted changes:

- **59 PostgreSQL cases passed**: indexing lifecycle plus existing storage guards,
  migrations, scan, ingestion, structural publication and recovery compatibility.
  The indexing readiness double in database tests is explicitly mocked.
- A separate terminal-accounting regression passed after strengthening assertions
  on source objects, submissions, active capacity and repeated recovery.
- **966 backend tests passed, 54 skipped**. The skips comprise 39 PostgreSQL
  cases and 15 opt-in native cases. PostgreSQL and indexer cases ran separately;
  standalone native source-reader cases were not rerun.
- **8 real Linux indexer cases passed**, including readiness, long valid metadata,
  128 files totaling exactly 16 MiB, long lines, redactions, cancellation,
  overflow and crash recovery. The capacity fixture took **12.642 seconds**,
  retained 4,096 chunks and correctly reported partial coverage. Sampled memory
  reached **120,795,955 bytes**, not a true peak measurement. Configured and
  inspected limits remain 1 GiB and 180 seconds.
- Strict typing passed for **221 application files**; Ruff passed. No frontend
  files or public response schemas changed; frontend checks were not rerun.

These checks qualify these synthetic fixtures, not every possible repository.
Consumer execution and atomic finalization are now covered by the
[worker publication qualification](index-publication.md). Sealed-result reads are covered by [qualified index reads](index-reads.md);
indexing controls and API/UI/MCP integration remain pending. No provider, AWS or deployment calls
were made. Changes remain local and uncommitted.
