"""FastAPI local literal registrations; dynamic composition abstains explicitly."""

import ast
from dataclasses import dataclass

from app.analyzers.frameworks import (
    METHODS,
    binding_for,
    diagnostic,
    handler_symbol,
    key,
    literal,
    location,
    method,
)
from app.analyzers.positions import SourceIndex
from app.contracts.structure import (
    AnalysisConfigV1,
    DiagnosticV1,
    RouteObservationV1,
    SourceLocationV1,
    SourceUnitV1,
    SymbolObservationV1,
)


@dataclass
class Router:
    prefix: str | None
    application: bool


def observe_fastapi(
    units: tuple[SourceUnitV1, ...],
    symbols: tuple[SymbolObservationV1, ...],
    config: AnalysisConfigV1,
) -> tuple[tuple[RouteObservationV1, ...], tuple[DiagnosticV1, ...]]:
    routes: list[RouteObservationV1] = []
    diagnostics: list[DiagnosticV1] = []
    for unit in sorted(units, key=lambda u: u.path):
        if unit.language != "python":
            continue
        index = SourceIndex(unit.data)
        try:
            tree = ast.parse(unit.data.decode())
        except (SyntaxError, ValueError, RecursionError):
            diagnostics.append(diagnostic("fastapi_parse_unavailable"))
            continue
        nodes = list(ast.walk(tree))
        if len(nodes) > config.limits.syntax_nodes_per_file:
            diagnostics.append(diagnostic("framework_syntax_budget"))
            continue
        constructors: dict[str, str] = {}
        modules: set[str] = set()
        routers: dict[str, Router] = {}
        mounts: dict[str, list[tuple[str, str | None, SourceLocationV1]]] = {}
        registrations: list[tuple[str, ast.Call, str | None, tuple[str, ...], str | None]] = []
        # Only module-level bindings establish framework object identity.
        for statement in tree.body:
            if isinstance(statement, ast.ImportFrom) and statement.module == "fastapi":
                for alias in statement.names:
                    if alias.name in ("FastAPI", "APIRouter"):
                        constructors[alias.asname or alias.name] = alias.name
            elif isinstance(statement, ast.Import):
                modules.update(a.asname or a.name for a in statement.names if a.name == "fastapi")
            elif (
                isinstance(statement, ast.Assign)
                and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name)
            ):
                name = statement.targets[0].id
                value = statement.value
                if isinstance(value, ast.Name):
                    if value.id in constructors:
                        constructors[name] = constructors[value.id]
                    elif value.id in routers:
                        routers[name] = routers[value.id]
                elif isinstance(value, ast.Call):
                    factory = (
                        constructors.get(value.func.id)
                        if isinstance(value.func, ast.Name)
                        else value.func.attr
                        if isinstance(value.func, ast.Attribute)
                        and isinstance(value.func.value, ast.Name)
                        and value.func.value.id in modules
                        else None
                    )
                    if factory in ("FastAPI", "APIRouter"):
                        prefix_node = next(
                            (k.value for k in value.keywords if k.arg == "prefix"), None
                        )
                        prefix = literal(prefix_node) if prefix_node else ""
                        routers[name] = Router(prefix, factory == "FastAPI")
                        if prefix is None:
                            diagnostics.append(
                                diagnostic("dynamic_router_prefix", location(unit, index, value))
                            )
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in statement.decorator_list:
                    if (
                        isinstance(decorator, ast.Call)
                        and isinstance(decorator.func, ast.Attribute)
                        and isinstance(decorator.func.value, ast.Name)
                        and decorator.func.value.id in routers
                    ):
                        verb = decorator.func.attr.upper()
                        if verb in METHODS or verb == "API_ROUTE":
                            methods = (verb,) if verb in METHODS else _methods(decorator)
                            registrations.append(
                                (
                                    decorator.func.value.id,
                                    decorator,
                                    literal(decorator.args[0]) if decorator.args else None,
                                    methods,
                                    statement.name,
                                )
                            )
            call = (
                statement.value
                if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)
                else None
            )
            if (
                call
                and isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id in routers
            ):
                owner = call.func.value.id
                if call.func.attr == "include_router":
                    child = (
                        call.args[0].id
                        if call.args and isinstance(call.args[0], ast.Name)
                        else None
                    )
                    prefix_node = next((k.value for k in call.keywords if k.arg == "prefix"), None)
                    prefix = literal(prefix_node) if prefix_node else ""
                    if child in routers:
                        mounts.setdefault(child, []).append(
                            (owner, prefix, location(unit, index, call))
                        )
                    else:
                        diagnostics.append(
                            diagnostic("router_binding_unresolved", location(unit, index, call))
                        )
                elif call.func.attr == "add_api_route":
                    path_node = (
                        call.args[0]
                        if call.args
                        else next((k.value for k in call.keywords if k.arg == "path"), None)
                    )
                    handler_node = (
                        call.args[1]
                        if len(call.args) > 1
                        else next((k.value for k in call.keywords if k.arg == "endpoint"), None)
                    )
                    registrations.append(
                        (
                            owner,
                            call,
                            literal(path_node),
                            _methods(call),
                            handler_node.id if isinstance(handler_node, ast.Name) else None,
                        )
                    )
        binding = binding_for(unit.path, config)
        for owner, call, path, methods, handler_name in registrations:
            registration = location(unit, index, call)
            pending: list[tuple[str, str, tuple[SourceLocationV1, ...], frozenset[str]]] = [
                (owner, "", (), frozenset())
            ]
            while pending:
                current, suffix, includes, seen = pending.pop()
                if current in seen or len(includes) >= 16:
                    diagnostics.append(diagnostic("router_mount_cycle_or_depth", registration))
                    continue
                router = routers[current]
                if router.prefix is None:
                    continue
                prefix = router.prefix + suffix
                if current in mounts:
                    for parent, mount_prefix, include in reversed(mounts[current]):
                        if mount_prefix is None:
                            diagnostics.append(diagnostic("dynamic_mount_prefix", include))
                            continue
                        pending.append(
                            (parent, mount_prefix + prefix, (include, *includes), seen | {current})
                        )
                    continue
                for verb in methods:
                    if len(routes) >= 256:
                        return tuple(routes), (*diagnostics[:499], diagnostic("route_budget"))
                    full_path = prefix + path if path is not None else None
                    if full_path is not None and (
                        not full_path.startswith("/") or len(full_path) > 2048
                    ):
                        full_path = None
                    if full_path is None or method(verb) == "UNKNOWN":
                        diagnostics.append(diagnostic("dynamic_route_path_or_method", registration))
                    handler = handler_symbol(symbols, unit, handler_name)
                    routes.append(
                        RouteObservationV1(
                            key=key(
                                unit,
                                "fastapi",
                                registration.range.start_byte,
                                verb,
                                full_path,
                                [i.model_dump() for i in includes],
                            ),
                            framework="fastapi",
                            service_key=binding.service_key if binding else None,
                            method=method(verb),
                            raw_path=path,
                            template_path=full_path,
                            registration=registration,
                            handler=handler.location if handler else None,
                            handler_key=handler.semantic_key if handler else None,
                            includes=includes,
                            state="resolved"
                            if full_path is not None and method(verb) != "UNKNOWN"
                            else "unresolved",
                            limitations=("static_registration_only",),
                        )
                    )
    return tuple(routes), tuple(diagnostics[: min(config.limits.diagnostics, 500)])


def _methods(call: ast.Call) -> tuple[str, ...]:
    value = next((k.value for k in call.keywords if k.arg == "methods"), None)
    if value is None:
        return ("GET",)
    if not isinstance(value, (ast.List, ast.Tuple)) or not value.elts or len(value.elts) > 7:
        return ("UNKNOWN",)
    return tuple(dict.fromkeys(literal(item) or "UNKNOWN" for item in value.elts))
