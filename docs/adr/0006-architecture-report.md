# ADR 0006: Evidence-first architecture report

Status: accepted for the deterministic report; model-written narrative is deferred. The
[acceptance record](../acceptance/architecture-report.md) owns verification evidence and limits.

## Decisions

1. **A report is a pure function of published facts.** It is computed from the AST facts of the shard
   analyses that cover a snapshot, stored once per inputs digest, and never updated or deleted. The
   database insert guard recomputes the inputs digest from the stored facts rows and refuses a report
   that cites a run which does not exist, is not a published AST-facts analysis of the same snapshot,
   overlaps another run, or disagrees with its own columns.
2. **Every statement is typed.** Following the evidence discipline of the REA project (observations,
   derivations, inferences and unknowns are kept distinct, provenance is carried, partial facts are
   kept, and coverage and limits are always reported), a statement is an *observation* (read from a
   fact, evidence required), a *derivation* (computed mechanically), an *inference* (a judgment the
   facts do not prove, such as "uses Flask") or an *unknown* (what the facts cannot settle). Only
   REA's methodology is adopted, not its code or runtime.
3. **Unresolved is unknown, never absent.** Imports that cannot be located, that match several files
   equally well, or that may live outside the analyzed set never create a dependency between two
   repository files; they are counted and listed as unknowns. A missing edge is not read as "no
   dependency".
4. **Statements carry templates and scalar parameters, evidence carries files and ranges.** Text is
   rendered deterministically from a template code, so no source value or quote can reach a report;
   only names, paths and line ranges do. This keeps the report safe to hand to a model later.
5. **Resolution is syntactic and conservative.** Per-language rules use only the analyzed paths and
   import facts. Python resolves only when a single file matches (or one sits beside the importer);
   path aliases, bundler settings, `go.mod` and build classpaths are not read.
6. **Coverage is explicit.** One AST-facts analysis reads at most 128 files, 16 MiB and 16 include
   prefixes, so the coordinator partitions a snapshot into disjoint shards (a pure function of the
   inventory, every analyzable path in exactly one shard) and creates one existing analysis per shard
   within the organization's pending-job quota. A report built from fewer files than are eligible says
   so in its data, its Markdown and the viewer.

## Deferred

A model-written narrative (component analysis and synthesis) is a separate phase. It will run as one
synthetic investigation per report so that the existing gateway, budget ledger, replay safety and
claim verifier apply unchanged, with every claim citing evidence the verifier can check, and the
narrative marked as inference unless verified. It needs the Bedrock runtime composed and qualified by
the owner first.
