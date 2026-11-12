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

- All thirteen registered languages are extracted. Files in other languages are never read as
  sources; the `language_unsupported` omission remains as a safeguard for a registered language
  without an extractor.
- C and C++ facts are syntactic: preprocessor conditionals are not evaluated, macro-generated
  declarations are not seen, and `.h` is treated as C. Swift class inheritance cannot be told from
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
