# Privacy-safe lexical materialization

> **Status reconciliation — 28 October 2026:** Read this component record with the later [index interfaces](index-interface.md) and [workspace verification](frontend-workspace.md). Preparation/materialization evidence alone does not qualify current API controls or the full product journey. No tests were rerun for this documentation update.

The materializer verifies each selected file's full admitted size and SHA-256,
rejects identity mismatches, and transforms the complete strict UTF-8 file before
chunking. Sensitive paths and unsupported encodings produce explicit omissions.
An empty eligible file is processed without inventing a chunk.

Half-open source ranges expand to include complete redaction placeholders.
Per-chunk provenance contains clipped, rebased intervals, original line ranges,
and original/safe digests. Raw source is not part of the returned draft.
The transform is deterministic and policy-based; it does not guarantee removal
of every possible secret.

Short files produce file chunks; larger inputs produce lexical windows.
The target is 768 tokens, with hard limits of 1,024 tokens and 4,096 safe UTF-8
bytes per chunk. Overlap is checked against the actual tokenizer and limited
to 64 tokens. Windows advance across long lines and never split a placeholder.
The builder does not infer functions or use structural graph hints.

Draft assembly orders files and chunks deterministically and accounts for
canonical serialized bytes incrementally. Bounds are 4,096 chunks, 16 MiB safe
content, 8 MiB manifest and 24 MiB draft. A conservative 256 KiB header reserve
can reduce packing; its actual size is checked too. Overflow is counted by
reason, with up to 128 scoped omission examples. A partial result does not
represent clean absence. Integrity or transform faults fail explicitly.

## Qualification boundary

Offline tests use the installed tokenizer and a SHA-256-verified cached
vocabulary. Synthetic canaries exercise shrinking, expanding, adjacent,
multiline and boundary-crossing redactions; UTF-8 boundaries, exact citations,
finite long-line coverage, deterministic identities, escaping, omitted input
and incremental row/serialization bounds are checked. A deliberately oversized
placeholder fixture is synthetic, not a normal transform output.

This library is not yet wired into the consumer, API or a container runtime.
These tests do not qualify sandboxing, peak memory, full-capacity execution,
provider-backed retrieval or production readiness. The existing chunker is
unchanged. Runtime preparation and durable job integration remain necessary.

## Observed checks

On 24 October 2026, reference `83767b105f0eb0870c79f84402241d729084120b`
plus the local changes:

- Materialization and contract selection: **64 passed**.
- Combined backend selection: **925 passed, 34 skipped**. The skips comprise
  27 database cases and seven opt-in native reader cases.
- The 27 PostgreSQL storage/publication cases passed separately before the
  pause; they were not rerun after adding the two path-format unit cases.
- Strict typing: **213 application files passed**. Ruff passed for application
  code and the materialization tests.

The later runtime qualification verified and removed the owned
`codespy-phase2-tests` container and network after Docker became available.
