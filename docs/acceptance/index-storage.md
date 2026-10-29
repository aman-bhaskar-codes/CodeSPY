# Index storage and publication guards

> **Status reconciliation — 28 October 2026:** This storage record predates the connected index workflow. Later [publication](index-publication.md), [reads](index-reads.md) and [interface](index-interface.md) evidence supersedes its integration-pending statements; its original test results remain unchanged. No tests were rerun for this documentation update.

Local qualification on 24 October 2026 used reference
`83767b105f0eb0870c79f84402241d729084120b` plus the uncommitted storage changes.
Historical revisions were preserved; `0023_index_publication` is additive.
Only marker-owned disposable PostgreSQL 16 databases were migrated.

## Implemented boundary

Four scoped tables hold canonical index specifications, attempt bindings,
immutable chunk sets and per-job publication receipts. Nullable membership and
safe-content provenance extend existing chunks, indexes and embedding rows;
legacy chunk IDs and stored memory pointers are retained. Historical job DTOs
remain independent of the new indexing projection.

The SQL guards require a live current worker attempt, current repository
write authority, matching retained admitted inputs, exact specification/config
identity and an immutable, complete publication. Each job needs its own terminal
receipt, including when it reuses an identical existing set. Different
configurations cannot reuse each other's results. Manifest membership is unique
and matches stored content, counts, safe bytes and coverage. The stored draft
digest is reconstructed from the committed manifest and content. Qualified
content cannot be appended, mutated or bypassed through NULL embedding
membership. External dense publication remains unavailable.

The typed repository owns no transactions. Its set lookup takes the existing
scope locks rather than requesting UPDATE privileges on immutable sets. Inserting
a parent alone cannot commit: the deferred checks require the complete result,
receipt and terminal job. These guards supplement application authorization;
this work adds no RLS or hosted database configuration.

Empty downgrade restores the previous schema. Downgrade with indexing history
is rejected. A real upgrade/downgrade/upgrade fixture retains a legacy chunk and
its memory pointer unchanged. The triage rollback regression now compares the
actual revision before/after the rejected downgrade, rather than assuming an
older head.

## Observed checks

- Indexing PostgreSQL selection: **27 passed**. Includes a complete legitimate
  publication, valid same-set reuse with separate terminal jobs, direct API-role
  restrictions, source/spec/profile mismatch, database-clock lease expiry,
  forged worker context, missing receipt, manifest duplicates, coverage mismatch,
  different-config reuse, NULL embedding membership and legacy preservation.
- Existing findings, triage, retrieval, migration and native-assisted source/HTTP
  compatibility selection against the new revision: **15 passed**.
- Combined backend selection: **905 passed, 34 skipped**. The 27 database cases
  skipped in that command ran separately above. Seven standalone native-reader
  cases were not rerun in this storage-only increment.
- Strict application typing: **209 source files passed**. Ruff passed for the
  application and affected tests/harness. No frontend behavior changed.

New migration tests first failed because the revision was absent. Subsequent
regressions exposed exact byte-count, profile, scope and five reviewed publication
invariant gaps before their fixes. Tests use the normal locked dependencies.
Intermittent original-checkout reads required a hash-checked source reconstruction;
tested files were mirrored and individually hash-matched to the checkout. Raw
logs and hashes remain local under ignored work files.

## Qualification limits

The SQL publication fixture uses a synthetic claim and synthetic prepared chunk
content. It qualifies the real PostgreSQL guard/role/transaction boundary, not
queue delivery, an indexing worker, privacy materialization or tokenizer output.
The new operation is not wired into the consumer or public API. This migration
alone does not activate durable indexing.

Full-size runtime/publication capacity, timeout and ambiguous-commit recovery
through the indexing finalizer, source-to-chunk execution, public status/search
integration and UI/MCP flows were pending at this storage-only qualification. No new model/provider behavior,
credentials, AWS access, provisioning, deployment or Git publishing was used.
