# Architecture report

An evidence-first architecture report is built from the published AST facts of a snapshot and stored
immutably. It has no model in the loop: every number and statement is computed from facts, every
statement is typed, and what the facts cannot settle is stated as unknown.

## Implemented boundary

- **Typed statements.** Observation (read from a fact, evidence required), derivation (computed
  mechanically), inference (a judgment such as "uses Flask" or "looks like an entry point") and unknown
  (unresolved or ambiguous imports, partial files, truncated coverage, fixed limitations). A statement
  is a template code plus scalar parameters with evidence references (file and range); no source value
  or quote can appear. Text is rendered from `GET /api/v1/architecture-report-templates`, so the
  server owns the wording.
- **Dependency graph** (`analyzers/graph/`). Per-language resolution of every import fact against the
  analyzed paths for Python, JavaScript/TypeScript, Go, Java, Rust, C/C++, Ruby, PHP and Swift. An edge
  is resolved, ambiguous (all candidates kept), external (standard library or third party) or
  unresolved (may be internal but cannot be located). Go and Swift imports target package nodes
  (directories). Python resolves only when one file matches, or one sits beside the importer.
- **Analytics.** From resolved edges only: file import cycles and component cycles, most-imported and
  most-importing files, directory-based components (single-child chains collapsed, at most 40),
  layers from the component condensation, entry-point candidates and external packages. Bounded
  computations report when they stop early.
- **Sections.** overview, components, dependencies, API surface, data model, configuration (names only),
  entry points, and coverage and limitations. A Markdown renderer and a D3-shaped nodes-and-links
  payload (paged) are served from the same stored document.
- **Coverage coordinator.** One AST-facts analysis reads at most 128 files, 16 MiB and 16 include
  prefixes. `POST .../architecture-reports/prepare` plans disjoint shards (a pure function of the
  inventory; every analyzable path in exactly one shard) and creates one existing analysis per shard,
  at most eight per call and within the organization's pending-job quota (call again for the rest).
  `GET .../prepare` aggregates shard status.
- **Storage.** Migration `0048_architecture_reports` adds an immutable table. The insert guard requires
  every cited run to be a published AST-facts analysis of the same snapshot, no file in two runs, the
  inputs digest recomputed from the stored facts rows to match, and the document to agree with its
  columns. One report per (repository, snapshot, inputs digest); updates and deletes are rejected.
- **Routes** (all tenant-scoped through the repository authorization): `POST .../prepare`,
  `GET .../prepare`, `POST .../architecture-reports` (builds once every shard is published, else 409
  `report_inputs_pending`), `GET .../latest`, `GET .../{id}`, `/markdown`, `/graph?offset&limit`,
  `/sections/{name}`.
- **Viewer.** The structural pane shows prepare and shard status, a build button, a partial-coverage
  banner, typed statements with evidence locations, tables, Markdown export and a layered SVG of
  component-to-component import counts. No new frontend dependency.

## Limitations

- Resolution is syntactic. Path aliases, bundler configuration, Cargo features, build, build
  classpaths and Swift package manifests are not read; such imports are unresolved, ambiguous or
  external, never guessed. Swift imports of the repository's own module resolve by directory name, and read as external only when the target name differs from the directory
  (Alamofire: 0 resolved of 191).
- Rust `mod x;` declarations are import facts (`self::x`, resolved to `x.rs` or `x/mod.rs`); inline
  modules are not. Files under `tests/`, `examples/` and `benches/` are their own crate roots. anyhow went from 27 to 50 resolved internal imports and from 7 to 1 unresolved.
- Component boundaries are a directory-based heuristic. Manifests are read for names only (see below); the
  stack is otherwise inferred from imports and framework facts.
- The model-written narrative (component analysis and synthesis) is not built; it needs the Bedrock
  runtime composed and qualified, and will attach to the report as inference unless verified.
- The coordinator relies on the existing analysis runtime; a large snapshot needs several shard jobs
  and the organization's pending-job quota (default 20) bounds how many exist at once.
- No retry or rebuild route: building again from the same facts returns the same report.

Manifest facts (names only, never executed, no new dependency):

- `Cargo.toml`, `package.json`, `pyproject.toml` and `Package.swift` are read as `manifest` file facts
  (`ManifestFactV1`: ecosystem, package name, dependency names with runtime/dev scope, workspace members,
  targets; capped at 512 dependencies). Versions, URLs, scripts and credentials are dropped. A manifest
  that does not parse is a partial file and the report says what it declares is unknown.
- They resolve own-package imports exactly: npm workspace package names to their directory and Cargo
  crate names (including workspace members) to the crate root. anyhow went from 50 to 71 resolved
  internal imports with its workspace crates recognised.
- Declared dependencies appear as observations with the manifest as evidence, plus a
  `declared_dependencies` table. Declared-versus-imported comparison is not made: import names and
  package names differ too often to state it without guessing.
- `go.mod` is parsed and resolves Go imports by the declared module path exactly, but only snapshots
  admitted under policy `source-zip-v4` carry it (see ast-facts acceptance); under V1 to V3 it is
  an unsupported file and Go keeps the inferred-prefix heuristic. The corpus runs under v4: gin's `go.mod` is read and its 31 internal imports resolve exactly as the heuristic had inferred. `Package.swift` is admitted by V3 and
  later. Adding manifests changes the facts digest of rows published afterwards.

Orphans and declared-versus-observed:

- **Orphan files** (no import edge in either direction, not an ambiguous candidate, not a test, example,
  docs or entry-like file) are reported only for Python, JavaScript/TypeScript and Rust, where a file must be
  named by an import to be used. Go, Java, PHP and Swift share packages implicitly, C and C++ files are
  compiled, and Ruby files are often autoloaded, so absence of an import proves nothing there. The statement
  is a derivation only when every import was located; otherwise it is an inference that says so.
- **Declared but not observed**: runtime dependencies of npm, Cargo and Go manifests with no matching import
  root, as an inference (build tooling and plugins legitimately look unused). Python distribution names and
  Swift package names differ from import names, so they are not compared.
- Manifest identity prefers the shallowest manifest and ignores those under examples, tests, fixtures and
  docs. Cargo `[lib] name` and `path` overrides are not read, so such crates resolve as external.

Architecture rules (on demand, never stored):

- `POST .../architecture-reports/{id}/rule-check` takes up to 16 rules (kinds `forbidden`, `layers`,
  `independence`; groups of path prefixes, same semantics as Import Linter's contracts) and evaluates them
  over the stored report's graph. Nothing is stored, so rules cannot change a report's identity.
- Only resolved imports are dependencies. Violations carry the import chain and the first import as
  evidence (at most 50 shown, the total is counted). A rule with no violation but unlocated imports from its
  files is `unknown`, not `passed`. `allow_indirect` limits forbidden and independence to direct imports.
- Same authorization as reading the report (repository scope, CSRF on the POST); malformed rules are 422.

Review follow-ups (documented bounds):

- Route handlers are reported only when they are plain dotted names; other expressions show as
  `<expression>`. Route paths keep their pattern, but long token-like segments are replaced.
- Shard job keys are scoped to the snapshot and lookups filter by repository and snapshot.
- Repositories needing more than 64 fact shards, or whose graph or report exceeds its size bound,
  are refused with `report_too_large` (HTTP 422); nothing is truncated silently.
- A failed or cancelled shard is retried by calling prepare again (a new analysis with an attempt-suffixed key).
  the viewer shows failures, stops polling, and the Prepare button retries them.
- Graph analytics are bounded by node, edge and scanned-edge counts, which decide every real outcome;
  the 120 s wall clock is only a safety net, so a report does not depend on machine speed for any input
  within the documented bounds (test: the outcome is identical under a 120 s and a 3600 s budget).
- `/graph` pages reuse a cached component grouping (16 reports, keyed by inputs digest).

## Verification

Owned disposable PostgreSQL 16 at schema head:

- **Unit:** contracts (typed statements, closed graphs, bounded documents), per-language resolution
  golden tests over real extracted facts, graph analytics, report assembly (typed statements, unknowns,
  no source value in the report, Markdown or graph, determinism, every emitted code has a template),
  shard plans (exactly-once coverage, 128 files, 16 prefixes, 15 MiB, determinism).
- **PostgreSQL:** prepare idempotent and quota-aware; build refused until every shard is published; one
  immutable report per inputs; the guard refuses missing runs, altered digests, document/column
  disagreement and duplicates; tenant isolation; HTTP flow with CSRF, key mismatch, 409, 404 and the
  section, graph and Markdown routes.
- **Frontend:** 76 tests including the viewer (prepare, build, refusal text, typed statements,
  evidence, partial-coverage banner, component graph).
- **Corpus** (`evals/ast/report_eval.py`, live marker, measured 2026-11-13, floors in `corpus.json`):

| Repository | Files | Resolved | Ambiguous | External | Unresolved | Components | File cycles | Report | Shards |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| pallets/flask | 83 | 256 | 77 | 291 | 5 | 6 | 2 | 266 KiB | 1 |
| expressjs/express | 142 | 160 | 0 | 245 | 0 | 30 | 0 | 180 KiB | 2 |
| gin-gonic/gin | 100 | 31 | 0 | 490 | 0 | 7 | 0 | 212 KiB | 1 |
| spring-projects/spring-petclinic | 50 | 24 | 0 | 451 | 0 | 6 | 0 | 269 KiB | 1 |
| dtolnay/anyhow | 39 | 71 | 0 | 76 | 1 | 7 | 1 | 63 KiB | 1 |
| antirez/kilo | 1 | 0 | 0 | 15 | 0 | 1 | 0 | 10 KiB | 1 |
| gabime/spdlog | 154 | 446 | 0 | 380 | 2 | 11 | 21 | 366 KiB | 2 |
| sinatra/sinatra | 147 | 130 | 0 | 62 | 0 | 8 | 0 | 110 KiB | 2 |
| slimphp/Slim | 72 | 112 | 0 | 242 | 0 | 8 | 1 | 170 KiB | 1 |
| Alamofire/Alamofire | 111 | 0 | 0 | 191 | 0 | 9 | 0 | 102 KiB | 1 |

  Every report built in 0.11 s or less. Running the corpus found three resolver defects now fixed with
  regression tests: Java treated a framework's shared directory prefix as internal, Go missed a
  module's root package, and a test fixture named like a library captured every importer in Python.
- **Regression:** the retrieval, job, accounting, memory, investigation, AST and model suites show the
  same 42 failures as the unchanged base branch (all pre-existing).
