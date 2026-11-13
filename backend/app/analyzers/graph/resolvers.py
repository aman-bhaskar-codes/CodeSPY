"""Per-language import resolution over analyzed paths. Syntax only, never guessing.

Each resolver maps one ``ImportFactV1`` to outcomes. A module found in exactly one analyzed file is
``resolved``; found in several equally close files is ``ambiguous`` (all candidates kept); a module
that cannot be in this repository (standard library, third-party) is ``external``; anything that
may belong to the repository but cannot be located (aliases, files outside the analyzed set,
generated code) is ``unresolved`` and therefore *unknown*, not absent.
"""

import sys
from collections.abc import Callable
from dataclasses import dataclass, field

from app.analyzers.graph.index import (
    FILE,
    PACKAGE,
    PathIndex,
    escapes,
    join,
    parent,
)
from app.contracts.ast_facts import ImportFactV1

MAX_CANDIDATES = 8


@dataclass(frozen=True)
class Outcome:
    resolution: str  # resolved | ambiguous | external | unresolved
    targets: tuple[str, ...] = ()
    external: str | None = None


@dataclass
class Ctx:
    path: str
    index: PathIndex
    state: dict[str, object] = field(default_factory=dict)

    @property
    def directory(self) -> str:
        return parent(self.path)


def file_id(path: str) -> str:
    return FILE + path


def pkg_id(directory: str) -> str:
    return PACKAGE + (directory or ".")


def _pick(ctx: Ctx, candidates: list[str]) -> Outcome | None:
    """Turn candidate paths into resolved/ambiguous, or None when there are none."""
    if not candidates:
        return None
    chosen = ctx.index.closest(ctx.path, sorted(set(candidates)))
    if len(chosen) == 1:
        return Outcome("resolved", (file_id(chosen[0]),))
    return Outcome("ambiguous", tuple(file_id(c) for c in chosen[:MAX_CANDIDATES]))


def _pick_strict(ctx: Ctx, candidates: list[str]) -> Outcome | None:
    """Resolve only when unambiguous: a lone candidate, or the lone one beside the importer.

    Python's import path depends on how the program is launched, so "nearest tree wins" would
    silently bind a test fixture named like a library to every test. Anything else stays ambiguous.
    """
    unique = sorted(set(candidates))
    if not unique:
        return None
    if len(unique) == 1:
        return Outcome("resolved", (file_id(unique[0]),))
    beside = [c for c in unique if parent(c) == ctx.directory]
    if len(beside) == 1:
        return Outcome("resolved", (file_id(beside[0]),))
    return Outcome("ambiguous", tuple(file_id(c) for c in unique[:MAX_CANDIDATES]))


def _external(root: str) -> Outcome:
    return Outcome("external", external=root)


UNRESOLVED = Outcome("unresolved")


# ---- Python ---------------------------------------------------------------------------------
def _py_module_files(ctx: Ctx, dotted: str) -> list[str]:
    path = dotted.replace(".", "/")
    return ctx.index.ending(path + ".py") + ctx.index.ending(path + "/__init__.py")


def python(imp: ImportFactV1, ctx: Ctx) -> list[Outcome]:
    outcomes: list[Outcome] = []
    names = [n.name for n in imp.names if n.name not in {"*", "self"}]
    if imp.relative_level:
        base = ctx.directory
        for _ in range(imp.relative_level - 1):
            base = parent(base)
        module = imp.module.replace(".", "/") if imp.module else ""
        anchor = join(base, module) if module else base
        found: list[str] = []
        if module:
            found = [p for p in (anchor + ".py", join(anchor, "__init__.py")) if ctx.index.has(p)]
        elif ctx.index.has(join(anchor, "__init__.py")):
            found = [join(anchor, "__init__.py")]
        if found:
            outcomes.append(Outcome("resolved", (file_id(found[0]),)))
        for name in names:
            sub_files = [
                p
                for p in (join(anchor, name + ".py"), join(anchor, name, "__init__.py"))
                if ctx.index.has(p)
            ]
            if sub_files:
                outcomes.append(Outcome("resolved", (file_id(sub_files[0]),)))
        return outcomes or [UNRESOLVED]
    module_outcome = _pick_strict(ctx, _py_module_files(ctx, imp.module))
    if module_outcome is not None:
        outcomes.append(module_outcome)
    for name in names:
        sub = _pick_strict(ctx, _py_module_files(ctx, f"{imp.module}.{name}"))
        if sub is not None:
            outcomes.append(sub)
    if outcomes:
        return outcomes
    root = imp.module.split(".")[0]
    if root in sys.stdlib_module_names:
        return [_external(root)]
    # A top-level name that exists as a directory or module somewhere in the repository may be
    # internal but unlocatable (not analyzed, generated): unknown, not external.
    internal = any(
        part == root or part == root + ".py"
        for p in ctx.index.inventory
        if p.endswith((".py", ".pyi"))
        for part in p.split("/")
    )
    return [UNRESOLVED if internal else _external(root)]


# ---- JavaScript / TypeScript ----------------------------------------------------------------
JS_EXTS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
JS_REMAP = {".js": (".ts", ".tsx"), ".jsx": (".tsx",), ".mjs": (".mts",), ".cjs": (".cts",)}
ALIAS_PREFIXES = ("@/", "~/", "#", "$", "@@/", "~~/")


def _js_file(ctx: Ctx, target: str) -> str | None:
    stem = target
    candidates = [target]
    for ext, remaps in JS_REMAP.items():
        if target.endswith(ext):
            stem = target[: -len(ext)]
            candidates += [stem + r for r in remaps]
    candidates += [target + e for e in JS_EXTS]
    candidates += [join(target, "index" + e) for e in JS_EXTS]
    for candidate in candidates:
        if ctx.index.has(candidate):
            return candidate
    return None


def javascript(imp: ImportFactV1, ctx: Ctx) -> list[Outcome]:
    spec = imp.module
    if spec.startswith(("./", "../")) or spec in {".", ".."}:
        if escapes(ctx.directory, spec):
            return [UNRESOLVED]
        found = _js_file(ctx, join(ctx.directory, spec))
        return [Outcome("resolved", (file_id(found),))] if found else [UNRESOLVED]
    if spec.startswith("node:"):
        return [_external(spec)]
    if spec.startswith(ALIAS_PREFIXES) or spec.startswith("/"):
        return [UNRESOLVED]  # path aliases and absolute paths need build configuration
    parts = spec.split("/")
    root = "/".join(parts[:2]) if spec.startswith("@") and len(parts) > 1 else parts[0]
    return [_external(root)]


# ---- Go -------------------------------------------------------------------------------------
def _go_dirs(ctx: Ctx) -> dict[str, list[str]]:
    dirs = ctx.state.get("go_dirs")
    if dirs is None:
        dirs = {d: ps for d, ps in ctx.index.by_dir.items() if any(p.endswith(".go") for p in ps)}
        ctx.state["go_dirs"] = dirs
    return dirs  # type: ignore[return-value]


def go_suffix_match(ctx: Ctx, path: str, minimum: int = 2) -> tuple[list[str], int] | None:
    """Directories matching the longest trailing run (at least ``minimum`` segments) of ``path``."""
    segments = path.split("/")
    dirs = _go_dirs(ctx)
    for k in range(len(segments), minimum - 1, -1):
        suffix = "/".join(segments[-k:])
        matches = sorted(d for d in dirs if d == suffix or d.endswith("/" + suffix))
        if matches:
            return matches, k
    return None


def go(imp: ImportFactV1, ctx: Ctx) -> list[Outcome]:
    path = imp.module
    segments = path.split("/")
    if "." not in segments[0]:
        return [_external(path)]  # standard library
    prefixes: set[str] = ctx.state.setdefault("go_prefixes", set())  # type: ignore[assignment]
    matched = go_suffix_match(ctx, path)
    if matched is None:
        # The module path is not parsed (go.mod is not admitted); infer it from imports that did
        # resolve: a path under a known module prefix names a directory relative to the root.
        for prefix in sorted(prefixes, key=len, reverse=True):
            if path.startswith(prefix + "/"):
                rest = path[len(prefix) + 1 :]
                if rest in _go_dirs(ctx):
                    return [Outcome("resolved", (pkg_id(rest),))]
                return [UNRESOLVED]
            if path == prefix:  # the module's root package
                if "" in _go_dirs(ctx):
                    return [Outcome("resolved", (pkg_id(""),))]
                return [UNRESOLVED]
        return [_external("/".join(segments[:3]))]
    dirs, k = matched
    if k < len(segments):
        prefixes.add("/".join(segments[: len(segments) - k]))
    if len(dirs) == 1:
        return [Outcome("resolved", (pkg_id(dirs[0]),))]
    return [Outcome("ambiguous", tuple(pkg_id(d) for d in dirs[:MAX_CANDIDATES]))]


# ---- Java -----------------------------------------------------------------------------------
JAVA_PLATFORM = ("java", "javax", "jakarta", "jdk", "sun", "com.sun", "org.w3c", "org.xml")


def java(imp: ImportFactV1, ctx: Ctx) -> list[Outcome]:
    dotted = imp.module
    if any(dotted == p or dotted.startswith(p + ".") for p in JAVA_PLATFORM):
        return [_external(dotted.split(".")[0])]
    as_path = dotted.replace(".", "/")
    if imp.kind == "wildcard":
        dirs = sorted(
            {
                d
                for d, files in ctx.index.by_dir.items()
                if (d == as_path or d.endswith("/" + as_path))
                and any(f.endswith(".java") for f in files)
            }
        )
        if len(dirs) == 1:
            return [Outcome("resolved", (pkg_id(dirs[0]),))]
        if dirs:
            return [Outcome("ambiguous", tuple(pkg_id(d) for d in dirs[:MAX_CANDIDATES]))]
    else:
        picked = _pick(ctx, ctx.index.ending(as_path + ".java"))
        if picked is not None:
            return [picked]
    parts = dotted.split(".")
    package = parts if imp.kind == "wildcard" else parts[:-1]
    head = "/".join(package[:3])
    # Only a package that names a directory of this repository can be internal; a shared prefix
    # such as org.springframework is not enough.
    internal = bool(head) and any(("/" + head + "/") in ("/" + d + "/") for d in ctx.index.by_dir)
    return [UNRESOLVED if internal else _external(".".join(parts[:2]))]


# ---- Rust -----------------------------------------------------------------------------------
def _rust_root(path: str) -> str | None:
    parts = path.split("/")
    for i in range(len(parts) - 1, -1, -1):
        if parts[i] == "src":
            return "/".join(parts[: i + 1])
    return None


def _rust_module_file(ctx: Ctx, root: str, chain: list[str]) -> str | None:
    """The file declaring the module ``root/chain...`` (``a.rs`` or ``a/mod.rs``), if analyzed."""
    base = join(root, *chain) if chain else root
    for candidate in (base + ".rs", join(base, "mod.rs")):
        if ctx.index.has(candidate):
            return candidate
    if not chain:
        for candidate in (join(root, "lib.rs"), join(root, "main.rs")):
            if ctx.index.has(candidate):
                return candidate
    return None


def rust(imp: ImportFactV1, ctx: Ctx) -> list[Outcome]:
    segments = imp.module.split("::")
    head = segments[0]
    if head not in {"crate", "self", "super"}:
        return [_external(head)]
    parent, _, filename = ctx.path.rpartition("/")
    if parent.rsplit("/", 1)[-1] in {"tests", "examples", "benches"} and filename.endswith(".rs"):
        # An integration test, example or bench file is its own crate root: `mod x;` is relative
        # to its directory.
        root, here = parent, []
    else:
        found = _rust_root(ctx.path)
        if found is None:
            return [UNRESOLVED]
        root = found
        stem = ctx.path[len(root) + 1 :]
        here = stem[: -len(".rs")].split("/")
        if here and here[-1] in {"mod", "lib", "main"}:
            here = here[:-1]
    if head == "crate":
        chain, rest = [], segments[1:]
    elif head == "self":
        chain, rest = here, segments[1:]
    else:
        chain = here[:]
        rest = segments[:]
        while rest and rest[0] == "super":
            if not chain:
                return [UNRESOLVED]
            chain = chain[:-1]
            rest = rest[1:]
    walked = list(chain)
    best = _rust_module_file(ctx, root, walked)
    for segment in rest:
        deeper = _rust_module_file(ctx, root, [*walked, segment])
        if deeper is None:
            break
        walked.append(segment)
        best = deeper
    if best is None or best == ctx.path:
        return [UNRESOLVED] if best is None else []
    return [Outcome("resolved", (file_id(best),))]


# ---- C / C++ --------------------------------------------------------------------------------
def c_family(imp: ImportFactV1, ctx: Ctx) -> list[Outcome]:
    name = imp.module
    if imp.relative_level:  # a quoted include names a project header
        direct = join(ctx.directory, name)
        if ctx.index.has(direct) and not escapes(ctx.directory, name):
            return [Outcome("resolved", (file_id(direct),))]
        picked = _pick(ctx, ctx.index.ending(name))
        return [picked or UNRESOLVED]
    picked = _pick(ctx, ctx.index.ending(name))  # angle includes may be project include roots
    return [picked or _external(name)]


# ---- Ruby -----------------------------------------------------------------------------------
def ruby(imp: ImportFactV1, ctx: Ctx) -> list[Outcome]:
    name = imp.module if imp.module.endswith(".rb") else imp.module + ".rb"
    if imp.relative_level:
        direct = join(ctx.directory, name)
        if ctx.index.has(direct) and not escapes(ctx.directory, name):
            return [Outcome("resolved", (file_id(direct),))]
        return [UNRESOLVED]
    picked = _pick(ctx, ctx.index.ending(name))
    return [picked or _external(imp.module.split("/")[0])]


# ---- PHP ------------------------------------------------------------------------------------
def php(imp: ImportFactV1, ctx: Ctx) -> list[Outcome]:
    if imp.kind == "require":  # literal include/require of a path
        direct = join(ctx.directory, imp.module)
        if ctx.index.has(direct) and not escapes(ctx.directory, imp.module):
            return [Outcome("resolved", (file_id(direct),))]
        return [UNRESOLVED]
    namespace = imp.module.replace("\\", "/")
    targets = [f"{namespace}/{n.name}" for n in imp.names] or [namespace]
    outcomes: list[Outcome] = []
    for target in targets:
        parts = target.split("/")
        # PSR-4 maps a namespace prefix (App\\) to a source directory, so the leading segments
        # may not appear in the path: keep the longest trailing run of at least two segments.
        for drop in range(max(1, len(parts) - 1)):
            picked = _pick(ctx, ctx.index.ending("/".join(parts[drop:]) + ".php"))
            if picked is not None:
                outcomes.append(picked)
                break
    if outcomes:
        return outcomes
    head = namespace.split("/")[0]
    internal = any(
        part.lower() == head.lower() for p in ctx.index.inventory for part in p.split("/")
    )
    return [UNRESOLVED if internal else _external(head)]


# ---- Swift ----------------------------------------------------------------------------------
def swift(imp: ImportFactV1, ctx: Ctx) -> list[Outcome]:
    module = imp.module.split(".")[0]
    dirs = sorted(
        d
        for d, files in ctx.index.by_dir.items()
        if (d == module or d.endswith("/" + module)) and any(f.endswith(".swift") for f in files)
    )
    if len(dirs) == 1:
        if dirs[0] == ctx.directory:
            return []  # a file importing its own module
        return [Outcome("resolved", (pkg_id(dirs[0]),))]
    if dirs:
        return [Outcome("ambiguous", tuple(pkg_id(d) for d in dirs[:MAX_CANDIDATES]))]
    return [_external(module)]


Resolver = Callable[[ImportFactV1, Ctx], list[Outcome]]
RESOLVERS: dict[str, Resolver] = {
    "python": python,
    "javascript": javascript,
    "jsx": javascript,
    "typescript": javascript,
    "tsx": javascript,
    "go": go,
    "java": java,
    "rust": rust,
    "c": c_family,
    "cpp": c_family,
    "ruby": ruby,
    "php": php,
    "swift": swift,
}
