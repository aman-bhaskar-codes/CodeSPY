"""Conservative lexical scopes and admitted local imports; never execute target code."""

import ast
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from app.contracts.structure import (
    AnalysisBudget,
    AnalysisConfigV1,
    DiagnosticV1,
    FileObservationsV1,
    ResolutionV1,
    SourceUnitV1,
)


@dataclass(eq=False)
class Scope:
    parent: "Scope | None"
    kind: str
    bindings: dict[str, list[ast.AST]] = field(default_factory=lambda: defaultdict(list))
    globals: set[str] = field(default_factory=set)
    nonlocals: set[str] = field(default_factory=set)


class Scopes(ast.NodeVisitor):
    def __init__(self, tree: ast.AST, budget: AnalysisBudget):
        self.scope = self.root = Scope(None, "module")
        self.owners: dict[ast.AST, Scope] = {}
        self.scopes = [self.root]
        self.budget = budget
        self.visit(tree)
        # Global/nonlocal assignments invalidate the corresponding outer binding too.
        for scope in self.scopes:
            for name in scope.globals | scope.nonlocals:
                writes = scope.bindings.pop(name, [])
                if writes:
                    target = self.root if name in scope.globals else self.outer(scope, name)
                    if target:
                        target.bindings[name].extend(writes)

    @staticmethod
    def outer(scope: Scope, name: str) -> Scope | None:
        parent = scope.parent
        while parent:
            if parent.kind != "class" and name in parent.bindings:
                return parent
            parent = parent.parent
        return None

    def visit(self, node: ast.AST) -> Any:
        self.budget.tick_nodes()
        self.owners[node] = self.scope
        return super().visit(node)

    def nested(self, node: ast.AST, kind: str, parameters: ast.arguments | None = None) -> None:
        previous = self.scope
        parent: Scope | None = previous
        if kind in {"function", "lambda", "comprehension"}:
            while parent and parent.kind == "class":
                parent = parent.parent
        self.scope = Scope(parent, kind)
        self.scopes.append(self.scope)
        if parameters:
            for arg in [
                *parameters.posonlyargs,
                *parameters.args,
                *parameters.kwonlyargs,
                *([parameters.vararg] if parameters.vararg else []),
                *([parameters.kwarg] if parameters.kwarg else []),
            ]:
                self.scope.bindings[arg.arg].append(arg)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for statement in node.body:
                self.visit(statement)
        else:
            self.generic_visit(node)
        self.scope = previous

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.scope.bindings[node.name].append(node)
        for expression in [
            *node.decorator_list,
            *node.args.defaults,
            *(d for d in node.args.kw_defaults if d is not None),
        ]:
            self.visit(expression)
        self.nested(node, "function", node.args)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.visit_FunctionDef(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.bindings[node.name].append(node)
        for expression in [*node.decorator_list, *node.bases]:
            self.visit(expression)
        self.nested(node, "class")

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self.nested(node, "lambda", node.args)

    def visit_ListComp(
        self, node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp
    ) -> None:
        self.nested(node, "comprehension")

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self.visit_ListComp(node)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self.visit_ListComp(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self.visit_ListComp(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.scope.bindings[node.id].append(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.scope.bindings[alias.asname or alias.name.split(".")[0]].append(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            self.scope.bindings[alias.asname or alias.name].append(node)

    def visit_Global(self, node: ast.Global) -> None:
        self.scope.globals.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.scope.nonlocals.update(node.names)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.scope.bindings[node.name].append(node)
        self.generic_visit(node)


def _module_paths(
    current: str, name: str | None, level: int, sources: Mapping[str, SourceUnitV1]
) -> tuple[str, ...]:
    directory = current.split("/")[:-1]
    if level:
        if level - 1 > len(directory):
            return ()
        base = directory[: len(directory) - level + 1] if level > 1 else directory
    else:
        base = []
    if name:
        base += name.split(".")
    stem = "/".join(base)
    return tuple(p for p in (stem + ".py", stem + "/__init__.py") if p in sources)


def resolve_python(
    files: Sequence[FileObservationsV1],
    sources: Mapping[str, SourceUnitV1],
    config: AnalysisConfigV1,
    budget: AnalysisBudget,
) -> tuple[tuple[ResolutionV1, ...], tuple[DiagnosticV1, ...]]:
    models = {}
    diagnostics = []
    observations = {f.path: f for f in files if f.language == "python"}
    for path in observations:
        try:
            tree = ast.parse(sources[path].data)
            models[path] = (tree, Scopes(tree, budget))
        except (SyntaxError, ValueError, RecursionError):
            diagnostics.append(
                DiagnosticV1(
                    code="resolution_unavailable",
                    stage="resolution",
                    message="Python lexical context is unavailable for this file.",
                )
            )
    lookup_count = 0

    def declaration(
        path: str, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
    ) -> list[str]:
        name = getattr(node, "name", None)
        matches = [
            s
            for s in observations[path].symbols
            if s.role == "declaration"
            and s.name == name
            and s.location.range.start_line <= node.lineno <= s.location.range.end_line
        ]
        if not matches:
            return []
        smallest = min(
            matches, key=lambda s: s.location.range.end_byte - s.location.range.start_byte
        )
        return [smallest.semantic_key]

    def lookup(
        path: str,
        scope: Scope | None,
        name: str,
        active: frozenset[tuple[str, int, str]] = frozenset(),
    ) -> tuple[list[str], Literal["lexical", "import_alias", "static_member", "unsupported"]]:
        nonlocal lookup_count
        lookup_count += 1
        key = (path, id(scope), name)
        if (
            scope is None
            or key in active
            or len(active) >= config.limits.import_depth
            or lookup_count > config.limits.module_lookups
        ):
            return [], "unsupported"
        owner = scope
        if name in scope.globals:
            owner = models[path][1].root
        elif name in scope.nonlocals:
            outer = Scopes.outer(scope, name)
            if outer is None:
                return [], "unsupported"
            owner = outer
        while name not in owner.bindings and owner.parent is not None:
            owner = owner.parent
        writes = owner.bindings.get(name, [])
        targets: list[str] = []
        basis: Literal["lexical", "import_alias", "static_member", "unsupported"] = "lexical"
        for node in writes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                targets.extend(declaration(path, node))
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                basis = "import_alias"
                for alias in node.names:
                    bound = alias.asname or (
                        alias.name.split(".")[0] if isinstance(node, ast.Import) else alias.name
                    )
                    if bound != name:
                        continue
                    paths = _module_paths(
                        path,
                        alias.name if isinstance(node, ast.Import) else node.module,
                        0 if isinstance(node, ast.Import) else node.level,
                        sources,
                    )
                    for target_path in paths:
                        if target_path not in models:
                            continue
                        if isinstance(node, ast.Import):
                            targets.extend(
                                s.semantic_key
                                for s in observations[target_path].symbols
                                if s.kind == "module"
                            )
                        else:
                            other, _ = lookup(
                                target_path, models[target_path][1].root, alias.name, active | {key}
                            )
                            targets.extend(other)
            else:
                return [], "unsupported"  # parameter, reassignment, exception or unknown receiver
        return sorted(set(targets)), basis

    rows: list[ResolutionV1] = []
    bykey = {s.semantic_key: s for f in files for s in f.symbols}
    for path, (tree, scopes) in models.items():
        calls = {(n.lineno, n.col_offset): n for n in ast.walk(tree) if isinstance(n, ast.Call)}
        imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
        for symbol in observations[path].symbols:
            budget.tick_nodes(0)
            if symbol.role not in {"call", "import"}:
                continue
            targets: list[str] = []
            basis: Literal["lexical", "import_alias", "static_member", "unsupported"] = (
                "unsupported"
            )
            kinds: tuple[Literal["binding", "call_target", "import"], ...]
            if symbol.role == "call":
                node = calls.get(
                    (symbol.location.range.start_line, symbol.location.range.start_col)
                )
                if node is not None:
                    scope = scopes.owners[node]
                    if isinstance(node.func, ast.Name):
                        targets, basis = lookup(path, scope, node.func.id)
                    elif isinstance(node.func, ast.Attribute) and isinstance(
                        node.func.value, ast.Name
                    ):
                        modules, _ = lookup(path, scope, node.func.value.id)
                        valid = bool(modules) and all(bykey[k].kind == "module" for k in modules)
                        if valid:
                            basis = "static_member"
                            for module in modules:
                                target_path = bykey[module].path
                                found, _ = lookup(
                                    target_path, models[target_path][1].root, node.func.attr
                                )
                                targets.extend(found)
                kinds = ("binding", "call_target")
            else:
                matching = [
                    n
                    for n in imports
                    if symbol.location.range.start_line
                    <= n.lineno
                    <= symbol.location.range.end_line
                ]
                if len(matching) == 1:
                    imported = matching[0]
                    for alias in imported.names:
                        name = alias.asname or (
                            alias.name.split(".")[0]
                            if isinstance(imported, ast.Import)
                            else alias.name
                        )
                        found, basis = lookup(path, scopes.owners[imported], name)
                        targets.extend(found)
                kinds = ("import",)
            targets = sorted(set(targets))
            if len(targets) > config.limits.candidates_per_site:
                targets = []
            for kind in kinds:
                if len(rows) >= config.limits.resolution_sites:
                    diagnostics.append(
                        DiagnosticV1(
                            code="candidate_budget",
                            stage="resolution",
                            message="Resolution record capacity reached.",
                        )
                    )
                    return tuple(rows), tuple(diagnostics[: config.limits.diagnostics])
                rows.append(
                    ResolutionV1(
                        site_key=symbol.semantic_key,
                        resolution_kind=kind,
                        status="ambiguous"
                        if len(targets) > 1
                        else "resolved"
                        if targets
                        else "unresolved",
                        candidate_keys=tuple(targets),
                        basis=basis,
                        reason="static_lexical_candidates"
                        if targets
                        else "dynamic_or_unavailable_binding",
                    )
                )
    return tuple(rows), tuple(diagnostics[: config.limits.diagnostics])
