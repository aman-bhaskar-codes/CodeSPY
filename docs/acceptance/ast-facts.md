# AST facts

Per-file syntactic facts are extracted in the offline Linux analysis container, re-validated by the
broker against the admitted archive, and published as immutable rows with the run and job.

## Implemented boundary

- **Request.** `POST /api/v1/repositories/{id}/ast-facts` takes `{snapshot_id, include_prefixes}`
  with a session, CSRF token and `Idempotency-Key`. It stores a structural analysis spec whose
  adapters are `("ast_facts",)` and enqueues the existing `analyze_structure` job. It is runtime
  gated like structural analysis (503 without a qualified toolchain or readiness).
- **Extraction (Python only).** Classes (bases, decorators), functions and methods (parameters with
  types, defaults and kinds, return type, `async`, decorators), module constants, structured
  imports (`import`, `from`, relative level, aliases, wildcard) and exports (`__all__` or public
  declarations). Local functions are not part of the module API surface. Other languages in the
  archive are listed as omissions with reason `language_unsupported`.
- **Evidence.** Every fact has a half-open byte range and line range. The broker re-reads the
  authenticated archive and rejects facts whose range is not exact, files outside the admitted
  source, and identity or coverage drift.
- **Publication.** `ast_file_facts` (migration `0043_ast_file_facts`) holds one immutable JSON row per
  file with its canonical digest. Rows are inserted in the fenced transaction that completes the
  run and job; the database guard requires a live worker fence, the current spec and the
  `ast_facts` adapter. Updates and deletes are rejected; downgrade is refused while rows exist.
- **Reads.** `GET .../snapshots/{id}/ast-facts` (counts by language), `.../ast-facts/files`
  (paged by path) and `.../ast-facts/files/{file_id}` (facts), all scoped by repository authorization.
- **Dispatch.** `ProfileDispatchOperation` routes `analyze_structure` jobs by configuration;
  structural jobs are unaffected.

## Limitations

- Only Python is extracted. Go, Rust, Java, C/C++, Ruby, PHP and Swift grammars are not installed and
  their files are not yet eligible at admission.
- Source reading, limits and timeouts are the structural ones: 128 files, 16 MiB, 180 s.
- Facts are syntactic; names are not resolved across files. Routes, models and configuration slots
  exist in the contract but are not populated yet.
- Files with syntax errors yield partial facts and are marked `partial`.
- Parsing runs one child process per file; throughput for large repositories is not measured.

## Verification

Run on owned disposable PostgreSQL 16 databases at schema head with the analysis image built from
this tree:

- extractor: signatures, parameter kinds, bases, decorators, imports, exports, byte/line ranges,
  determinism, syntax-error tolerance and node budget
- pipeline and broker validation: identity, coverage and range tampering rejected; unsupported
  languages reported as omissions
- publication: dispatch, atomic rows with the terminal run/job, immutability, tenant isolation,
  refusal of structural specs, structural jobs still routed to the structural operation, and
  downgrade refused with history
- API: create is gated and idempotent, reads are paged and scoped
- container: real sandbox output equals in-process extraction and passes broker validation
- regression: structural container, existing suites, `ruff`, `mypy`, OpenAPI/SDK and frontend checks
