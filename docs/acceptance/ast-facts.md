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

## Toolchain and admission

- **Grammars.** Tree-sitter grammars for Go (0.25.0), Rust (0.24.2), Java (0.23.5), C (0.24.2),
  C++ (0.23.4), Ruby (0.23.1), PHP (0.25.1) and Swift (0.7.4) are pinned exactly beside the original
  Python, JavaScript and TypeScript grammars, in `backend/pyproject.toml` and `uv.lock`, and in the
  analysis and qualification image builds, which install hash-locked binary wheels only.
  `tree-sitter-language-pack` stays dormant (ADR 0003). Every grammar loads under tree-sitter
  0.26.0 with a compatible ABI and parses a fixture, including original byte ranges after a
  non-ASCII, CRLF prefix.
- **Registry.** `analyzers/registry.py` holds one table of languages (extensions, grammar module,
  pinned distribution). Structural analysis keeps exactly its original dialects; facts see all
  thirteen. The structural TypeScript checker now selects units by JavaScript/TypeScript language
  instead of "anything that is not Python".
- **Admission policy `source-zip-v3`.** Admits `.go .rs .java .c .h .cc .cpp .cxx .hpp .hh .hxx .rb
.php .swift` with language hints and keeps the V2 `uv.lock` metadata rules. The default policy is
  unchanged, so existing admissions, digests and snapshots are unaffected; V1 and V2 still mark these
  files `unsupported_language`. Snapshots admitted under V1 or V2 must be re-admitted to analyze
  these languages. `.h` is classified as C.
- **Enabling V3.** Set `ApplicationPorts.admission_policy` to `AdmissionPolicyV3()` (and pass a V3
  policy to `GitHubAcquisitionService`). `scripts/phase2_local.py` does both when
  `CODESPY_ADMISSION_POLICY=v3`.

## Limitations

- Only Python is extracted. The other grammars are installed and their files are admitted under
  policy V3, but facts for them are not implemented yet: they appear as `language_unsupported`
  omissions.
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
- toolchain: all thirteen grammars load and parse inside the analysis image; V3 admission verified
  in the real admission container; registry, admission tables and pins agree
- regression: structural container, existing suites, `ruff`, `mypy`, OpenAPI/SDK and frontend checks
