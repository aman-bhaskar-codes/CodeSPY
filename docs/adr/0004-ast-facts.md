# ADR 0004: Evidence-first AST facts

Status: accepted; Python implemented. The [acceptance record](../acceptance/ast-facts.md) owns
verification evidence and limits.

## Decisions

1. Add per-file syntactic facts (symbols with signatures, base classes and decorators, structured
   imports, exports, and slots for routes, models and configuration reads) beside the structural
   report instead of widening it. `structural-small-v1` contracts, hashes and tables stay frozen.
2. Select the pipeline by the `ast_facts` adapter in the existing `AnalysisConfigV1`. The spec, job,
   claim-time run, source locator, source reader, sandbox image and runner are reused; the adapter is
   not in the default adapter tuple, so existing configuration hashes are unchanged. A dispatcher
   routes `analyze_structure` jobs by configuration, so the consumer is unchanged.
3. Every fact carries a source range. The broker re-reads the admitted archive and rejects any fact
   whose range is not a faithful UTF-8/UTF-16 range of the cited file, any file outside the admitted
   source, and any identity or coverage drift. Container output is never trusted.
4. Store one immutable row per file in `ast_file_facts`, written in the same fenced transaction that
   completes the run and job. The insert guard mirrors the structural report guard and additionally
   requires the `ast_facts` adapter in the stored spec.
5. Facts are deterministic: a canonical digest per file; extraction is iterative, budgeted, and runs
   in a per-file child process inside the offline container. The progress-callback API stays banned
   (ADR 0003, decision 5).
6. Facts are syntax only. No cross-file name resolution is claimed, and configuration reads record
   names, never values.
7. Install new grammars as individually pinned, hash-locked wheels, never through the dormant
   language pack. Admit their source files only under a new admission policy version
   (`source-zip-v3`): changing eligibility under an existing policy digest would give one archive
   and policy two different manifests. Existing policies and digests stay byte-for-byte unchanged.
