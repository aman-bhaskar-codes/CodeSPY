# Qualified index reads

Exact, lexical, dense and hybrid services select one committed lexical chunk set
with a scoped publication receipt. Results identify the set, configuration,
materialization, safe-content digest and admitted-file coverage. A qualified
empty set is available with zero hits; a missing index is explicitly unavailable.
Unassociated legacy chunks remain stored but are excluded from these read paths.

Hybrid requests pin a set before dispatching channels. Each channel receives the
same pin; context preserves it rather than selecting a newer publication.
Conflicting pins, foreign sets, unqualified evidence and corrupt safe-content
digests fail closed. Current source retention, database-time expiry and repository
authority are rechecked after awaited reads. Qualified immutable content cannot
be cleared or resealed by legacy lifecycle helpers.

Chunk-backed memory records preserve their historical set and stored IDs. Reads
validate that same set and source; unavailable references are filtered without
rewriting history. Non-chunk memory policy remains unchanged. Memory result
cursors close on normal completion, validation errors and cancellation.

Existing HTTP routes replace client tenant/principal/repository/snapshot identity
with authenticated route identity while retaining an optional set pin. Context
created from automatic search uses that search's pinned scope. Indexing
controls, generated client types and the read-only MCP status tool are implemented
locally; their connected PostgreSQL/browser qualification is recorded in the
[index interface checks](index-interface.md).

## Verification checkpoint

On 24 October 2026, reference
`83767b105f0eb0870c79f84402241d729084120b` plus local uncommitted changes:

- Current local backend and synthetic evaluation selection: **1,008 passed,
  87 skipped**, in 23.67 seconds. The skips include opt-in database and native
  checks; this is not a claim that they ran in that command.
- Strict typing passed for **225 application files**. Ruff passed for application
  code and touched tests. No frontend or generated-schema checks ran.
- Before the pause, **40 affected PostgreSQL/native integration checks passed**,
  with one separately qualified maximum-publication case skipped. Those results
  cover actual immutable publication, source expiry, legacy exclusion, a new
  publication between channels, historical memory identity and genuine empty
  admitted input. The real isolated reader/indexer canary journey passed through
  search and context with bounded output and correct original citations.
- After Docker became available, the final affected PostgreSQL/native run passed
  **45 tests**, with one maximum-publication case skipped, in 70.35 seconds. This
  includes the additional building/failed/stale legacy-index cases, partial
  parent coverage and an existing foreign materialization. Native canary
  admission-to-context and publication compatibility checks ran in that command.
  The skipped maximum-publication fixture was qualified separately in the
  [publication record](index-publication.md); it was not rerun here.
- The older ranking evaluation now explicitly uses simulated selection/context
  fixtures and a consistent simulated provider identity. Relevance thresholds
  remain unchanged. It does not qualify a real embedding model or publication
  authority.

The current work has passed an independent scoped read review. The whole-change
review and final qualification gate remain pending. No new dependencies,
credentials, providers, AWS access, GitHub changes or deployment were performed.
Changes remain local. Public indexing activation stays disabled.

The final focused gate passed **213 tests** in 32.15 seconds, including real
PostgreSQL/native retrieval, contract, channel, context, memory, API and synthetic
evaluation checks. Owned database/emulator containers and network were removed;
final test-cluster, source-reader and indexer inventories were empty. Developer
volumes were preserved. Trusted images remain idle.
