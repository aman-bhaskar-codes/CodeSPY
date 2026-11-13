# AST facts

Per-file syntactic facts are extracted in the offline Linux analysis container, re-validated by the
broker against the admitted archive, and published as immutable rows with the run and job.

## Implemented boundary

- **Request.** `POST /api/v1/repositories/{id}/ast-facts` takes `{snapshot_id, include_prefixes}`
  with a session, CSRF token and `Idempotency-Key`. It stores a structural analysis spec whose
  adapters are `("ast_facts",)` and enqueues the existing `analyze_structure` job. It is runtime
  gated like structural analysis (503 without a qualified toolchain or readiness).
- **Extraction.** Python, JavaScript, JSX, TypeScript and TSX. Each file yields symbols with kind,
  qualified name, visibility, modifiers, decorators, signature (parameters with type, default,
  kind and optionality; return type), base classes, implemented interfaces and an `exported` flag;
  structured imports; and exports.
  - _Python:_ classes (enum and `Protocol` classes get their own kinds), functions and methods,
    class fields (annotated or plain), module constants, `TypeAlias` and `type` aliases; `import`,
    `from`, relative level, aliases, wildcard and `__future__` imports, including those inside
    `if`/`try`/`with` blocks (under `TYPE_CHECKING` they are `type_only`) and literal
    `importlib.import_module()` / `__import__()` calls as `dynamic`; exports from `__all__` or
    public declarations. Local functions are not part of the API surface.
  - _JavaScript and TypeScript:_ functions, arrow and function-expression bindings, classes
    (`extends`, `implements`, static/async/generator/getter/setter/abstract/readonly modifiers,
    `public`/`private`/`protected` and `#private`), fields, interfaces, type aliases, enums,
    namespaces, ambient declarations and `UPPER_CASE` or exported constants; static, namespace,
    default, side-effect and `import type` imports, `require()` and literal `import()` calls;
    named, default and re-exports (`export * from`, `export { a as b } from`) and CommonJS
    `module.exports` / `exports.x`.
  - _Go:_ structs (embedded types as bases), interfaces (embedded interfaces and method elements),
    defined types and aliases, methods (receiver type as container, `pointer_receiver` modifier),
    functions (including generic), parameters (grouped `a, b int` expanded, variadic), results,
    constants and variables, fields, exported names by capitalization (`public` versus `internal`),
    and structured imports (aliased, dot, blank).
  - _Java:_ classes, interfaces, enums (constants as fields), records (components as fields) and
    annotation types, nested types, fields (constants for `static final` and interface constants),
    methods and constructors, annotations as decorators, `extends`/`implements`, access levels
    (package-private is `internal`; interface members are `public`), and plain, static and
    wildcard imports. Public top-level types are exports.
  - _Rust:_ structs (named and tuple fields), enums (variants), traits (supertrait bounds),
    `impl` blocks (methods keyed to the impl type; `impl Trait for T` adds the trait to the type's
    `implements`), functions with `async`/`const`/`unsafe`, constants, statics (`mut`), type
    aliases, nested `mod` items, attributes as decorators, `pub`/`pub(crate)`/private visibility,
    and `use` declarations (grouped, aliased, glob, `pub use` as re-exports).
  - _C and C++:_ structs, unions and classes (base classes, access sections), enums (enumerators
    as fields), typedefs (anonymous aggregates, function-pointer types), `using` aliases, functions
    and prototypes, methods (including out-of-class `ns::Foo::run` definitions, constructors,
    destructors, trailing `const`/`override`), templates, namespaces, variables and constants,
    object-like and function-like macros, parameter types with `*`/`&`/`&&`, defaults and
    variadics, and `#include` (quoted includes have `relative_level` 1). In headers every non-static
    top-level declaration is an export; in sources only external-linkage functions and variables.
  - _Ruby:_ modules (namespaces), classes with superclass, `include`/`prepend` as `implements` and
    `extend` as a decorator, `attr_accessor`/`attr_reader`/`attr_writer` fields, methods and
    singleton methods, parameter kinds (optional, splat, keyword, hash-splat, block), visibility
    from bare `private`/`protected`/`public`, `private :name` and `private def`, constants, and
    `require`/`require_relative`/`load`.
  - _PHP:_ namespaces, classes (abstract/final, `extends`, `implements`, composed traits),
    interfaces, traits, enums (backing type, cases), constants, properties, methods with
    visibility and modifiers, constructor-promoted properties, typed, optional, variadic and
    default parameters, functions, `use` (simple, grouped, aliased) and literal
    `include`/`require`.
  - _Swift:_ classes, structs, enums (cases), actors, protocols, extensions (members keyed to the
    extended type; conformances merged onto the type), properties (`let`/`var`), methods and
    initializers with `async`/`throws`, labelled parameters (`with y`), defaults and variadics,
    attributes as decorators, access levels (`public`/`open` are public), type aliases, global
    constants and variables, and imports.
- **Framework facts.** HTTP routes (`framework`, method, path, handler) and ORM models (framework,
  table, columns with type, nullability, primary key and foreign key) for the most common
  frameworks. Each pass is gated by the file's own imports, so unrelated files carry none.
  - _Python:_ Flask and FastAPI decorators (`route` with `methods=` expands one route per method;
    `get`/`post`/... on apps, blueprints and routers), Django `urlpatterns` (`path`, `re_path`,
    `url`, `include`; method `ANY`), SQLAlchemy models (classic `Column` and 2.0
    `Mapped[...]`/`mapped_column`, `ForeignKey`, `__tablename__`) and Django models (field types,
    `null`, `primary_key`, `ForeignKey`/`OneToOneField`, `Meta.db_table`).
  - _JavaScript and TypeScript:_ Express `app`/`router` verbs, `all` and `use` with a literal path.
  - _Java:_ Spring `@RequestMapping` (class prefix joined to method paths) and `@Get/Post/Put/
Delete/PatchMapping`; JPA `@Entity`/`@Table`/`@Id`/`@Column`/`@JoinColumn`, skipping static and
    `@Transient` fields.
  - _Go:_ Gin, Echo, chi, Fiber and gorilla/mux verb calls, `net/http` `HandleFunc`/`Handle`
    (including `"GET /path"` patterns), and gorm models (`gorm.Model`, `column:`, `primaryKey`,
    `not null`, `foreignKey:`, `gorm:"-"`, `TableName()`).
  - _Reads:_ `GET .../snapshots/{id}/ast-facts/routes` and `.../models` list them across a
    snapshot, paged by file path and ordinal.
- **Configuration reads.** Environment variables (`env_var`) and configuration keys
  (`config_key`), recorded by **name only**: defaults, fallbacks and values are never stored, and
  computed names are skipped. A byte-marker check skips the second parse for files that cannot
  contain a read.
  - _Environment:_ Python `os.environ[...]`, `os.environ.get/setdefault/pop`, `os.getenv`;
    JavaScript and TypeScript `process.env.X`, `process.env["X"]`, `import.meta.env.X`,
    `Deno.env.get`; Go `os.Getenv`/`LookupEnv`; Java `System.getenv`; Rust `std::env::var`,
    `var_os`, `env!`, `option_env!`; Ruby `ENV[...]`, `ENV.fetch`; PHP `getenv`, `env()`,
    `$_ENV[...]`; Swift `ProcessInfo.processInfo.environment[...]`; C and C++ `getenv`.
    `$_SERVER` is request data and is not treated as configuration.
  - _Keys (import-gated):_ Django `settings.X`, Flask `app.config[...]`/`.get`, node-config
    `config.get/has`, viper `GetString`-style calls, Java `System.getProperty`, Spring
    `@Value("${key}")` and `@ConfigurationProperties(prefix=...)`, Laravel `config()`.
  - _Placeholder defaults:_ Spring `${name:default}` is masked to `${name:…}` in stored
    annotation text, since the default can be a fallback credential.
  - _Reads:_ `GET .../snapshots/{id}/ast-facts/config` lists them across a snapshot.
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
- **Admission policy `source-zip-v4`.** V3 plus `go.mod`, which is read only as a manifest fact (module
  path and required module names, no versions). `go.sum` and every other file type stay unsupported. V1 to V3 are
  unchanged, so their digests and snapshots are unaffected; a snapshot admitted under V3 must be
  re-admitted to have its `go.mod` read. Enable with `AdmissionPolicyV4()` or
  `CODESPY_ADMISSION_POLICY=v4` in `scripts/phase2_local.py`.
- **Enabling V3.** Set `ApplicationPorts.admission_policy` to `AdmissionPolicyV3()` (and pass a V3
  policy to `GitHubAcquisitionService`). `scripts/phase2_local.py` does both when
  `CODESPY_ADMISSION_POLICY=v3`.

## Limitations

- All thirteen registered languages are extracted. Files in other languages are never read as
  sources; the `language_unsupported` omission remains as a safeguard for a registered language
  without an extractor.
- C and C++ facts are syntactic: preprocessor conditionals are not evaluated, macro-generated
  declarations are not seen, and `.h` is admitted as C (a header that reads with fewer errors under
  the C++ grammar is parsed with it; its fact language stays `c`). Swift class inheritance cannot be told from
  protocol conformance syntactically, so a class lists everything in `bases`. PHP `use function` and
  `use const` imports are not distinguished from class imports. Ruby metaprogramming (`define_method`,
  `Struct.new`) is not followed.
- Go and Rust module names are derived from the file path (Go: the directory), not from `go.mod`
  or `Cargo.toml`. Go struct tags, Java generics bounds and Rust macros, `impl` generics and
  `where` clauses are not recorded; Rust `macro_rules!` items are skipped.
- Destructured `require()` bindings record the module but not the destructured names, dynamic
  imports with non-literal arguments are not recorded, `__all__ +=` is not followed, and
  JavaScript values exported through arbitrary expressions are recorded only as `default`.
- Source reading, limits and timeouts are the structural ones: 128 files, 16 MiB, 180 s.
- Facts are syntactic; names are not resolved across files.
- Files with syntax errors yield partial facts and are marked `partial`.
- Parsing runs one child process per file. The 128-file cap means a real repository is analyzed
  only in part (see the corpus below); a wider profile is future work.

- Framework detection is syntactic and import-gated: routers registered through helper functions,
  `Group` prefixes (Gin, Echo), Express sub-router mounting beyond the literal `use` path, Django
  `include()` expansion, Spring `value` arrays beyond literals, SQLAlchemy relationships and
  declarative mixins, and Django abstract-model inheritance are not resolved. Other frameworks
  (NestJS, Koa, Rails, Laravel, Axum, Vapor, ...) are not covered.

- Facts quote source text: parameter defaults, decorator and annotation arguments and constant
  initializers are stored as written, so a secret hard-coded in source is visible to anyone who can
  read the snapshot's facts. Only configuration _reads_ are guaranteed value-free. Facts must be
  redacted before they leave the deployment (for example in model prompts).
- Configuration is detected from direct calls and subscripts; reads through wrapper helpers,
  settings objects (pydantic `BaseSettings`, Spring `Environment` beans other than `getProperty`),
  `.env` files and dotenv loaders are not followed.

## Real-repository corpus

`evals/ast/corpus.json` pins ten public repositories by full commit (Flask, Express, Gin, Spring
PetClinic, anyhow, kilo, spdlog, Sinatra, Slim, Alamofire). Nothing is vendored. `scripts/ast_corpus.py
resolve|fetch|run` pins, downloads (through the importer's fixed-origin, credential-less transport)
and measures; archives are cached under the git-ignored `.local/`. The runner admits each archive
under policy V3 with the GitHub root strip and extracts every eligible file in-process, which runs
the same code as the sandbox, so parser quality is measured without the 128-file production cap.
`tests/corpus` checks floors per repository (parse success, clean-parse ratio, symbols, routes,
models, pinned commit, no crashes). The live test needs `CODESPY_LIVE_CORPUS=1`; the harness itself
is covered offline.

Measured on 2026-11-12 (Apple silicon, one thread, in-process; `benchmarks/ast-corpus-2026-11-12.json`):

| Repository | Language | Files | Parsed | Clean | kLOC | files/s | MiB/s | Within 128-file cap |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| pallets/flask | Python | 83 | 100% | 100% | 18.5 | 227 | 1.5 | 100% |
| expressjs/express | JavaScript | 142 | 100% | 100% | 22.2 | 595 | 2.3 | 90% |
| gin-gonic/gin | Go | 99 | 100% | 100% | 24.7 | 160 | 1.1 | 100% |
| spring-projects/spring-petclinic | Java | 50 | 100% | 100% | 4.3 | 691 | 1.9 | 100% |
| dtolnay/anyhow | Rust | 37 | 100% | 94.6% | 5.9 | 297 | 1.5 | 100% |
| antirez/kilo | C | 1 | 100% | 100% | 1.3 | 48 | 1.9 | 100% |
| gabime/spdlog | C/C++ | 154 | 100% | 18.8% | 33.4 | 116 | 0.9 | 83% |
| sinatra/sinatra | Ruby | 147 | 100% | 100% | 24.3 | 461 | 2.0 | 87% |
| slimphp/Slim | PHP | 72 | 100% | 100% | 5.9 | 1197 | 2.6 | 100% |
| Alamofire/Alamofire | Swift | 111 | 100% | 95.5% | 47.6 | 125 | 2.4 | 100% |

"Parsed" means a tree and facts were produced (possibly `partial`); "Clean" means no syntax error
nodes. No file crashed extraction. What the corpus found and what remains:

- **C++ headers named `.h`** were read with the C grammar (spdlog: about 15,000 error nodes). They
  now fall back to the C++ grammar when it reads the file with fewer errors (about 1,600 remain).
- **Unexpanded macros** cause the remaining errors: spdlog wraps namespaces in
  `SPDLOG_NAMESPACE_BEGIN/END`, anyhow's `macro_rules!` bodies are not Rust, and Swift's
  `#error` directive and some postfix `!` forms are not accepted by the grammar. Facts are still
  produced for the rest of each file; the clean ratio for such repositories is low by nature.
- **Frameworks:** Flask (40 routes), Gin (13), Spring PetClinic (17 routes, 9 models) are found.
  Express, Sinatra and Slim define routes through their own APIs (`app.get = function`, `get '/'`,
  `$app->get()` on non-imported receivers), which are outside the covered frameworks. Express itself
  is a framework, so assignment-defined functions (`app.use = function`) are not symbols.
- **Capacity:** with the 128-file production cap, 4 of 10 repositories would be analyzed only in
  part (Express 90%, Sinatra 87%, spdlog 83%). Lifting the cap needs the wider-limit profile.
- Throughput is single-threaded in-process parsing, not the sandbox; per-file child processes are
  slower.

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
