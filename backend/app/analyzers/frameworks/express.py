"""Literal Express application/router composition; middleware is not an endpoint."""

from app.analyzers.frameworks import (
    binding_for,
    diagnostic,
    js_args,
    js_imports,
    js_literal,
    js_text,
    js_tree,
    key,
    location,
    member,
    method,
    module_level,
    stable_js_name,
    walk,
)
from app.analyzers.positions import SourceIndex
from app.contracts.structure import (
    AnalysisConfigV1,
    DiagnosticV1,
    RouteObservationV1,
    SourceUnitV1,
    SymbolObservationV1,
)
from tree_sitter import Node


def observe_express(
    units: tuple[SourceUnitV1, ...],
    symbols: tuple[SymbolObservationV1, ...],
    config: AnalysisConfigV1,
) -> tuple[tuple[RouteObservationV1, ...], tuple[DiagnosticV1, ...]]:
    routes, diagnostics = [], []
    for unit in sorted(units, key=lambda u: u.path):
        if unit.language == "python":
            continue
        root, error = js_tree(unit, config)
        if root is None or error:
            diagnostics.append(diagnostic("express_parse_unavailable"))
            continue
        index = SourceIndex(unit.data)
        factories = {
            name
            for name in js_imports(root, unit, "express")
            if stable_js_name(root, root, name, unit)
        }
        objects: dict[str, bool] = {}
        for node in walk(root):
            if node.type != "variable_declarator" or not module_level(node):
                continue
            name, value = node.child_by_field_name("name"), node.child_by_field_name("value")
            if (
                not name
                or name.type != "identifier"
                or not value
                or value.type != "call_expression"
            ):
                continue
            fn = value.child_by_field_name("function")
            if js_text(fn, unit) in factories:
                objects[js_text(name, unit)] = True
            elif member(fn, unit) in {(factory, "Router") for factory in factories}:
                objects[js_text(name, unit)] = False
        mounts: dict[str, list[tuple[str, str | None, Node]]] = {}
        registrations: list[tuple[str, str, str | None, Node]] = []
        for node in walk(root):
            if node.type != "call_expression":
                continue
            expression = member(node.child_by_field_name("function"), unit)
            if (
                not expression
                or expression[0] not in objects
                or not stable_js_name(root, node, expression[0], unit)
            ):
                continue
            owner, verb = expression
            args = js_args(node)
            if not args or verb.lower() not in {
                "get",
                "post",
                "put",
                "patch",
                "delete",
                "head",
                "options",
                "all",
                "use",
            }:
                continue
            path = js_literal(args[0], unit)
            if path is not None and any(c in path for c in "*?()+"):
                path = None
            if verb == "use":
                child = js_text(args[1], unit) if len(args) == 2 else ""
                if child in objects and not objects[child]:
                    mounts.setdefault(child, []).append((owner, path, node))
                    continue
                # Retain uncertainty for unsupported middleware, never invent an endpoint.
                diagnostics.append(
                    diagnostic("express_middleware_unresolved", location(unit, index, node))
                )
                if path is None:
                    registrations.append((owner, verb, None, node))
                continue
            registrations.append((owner, verb, path, node))

        def prefixes(
            owner: str,
            active: frozenset[str] = frozenset(),
            known: dict[str, bool] = objects,
            parents: dict[str, list[tuple[str, str | None, Node]]] = mounts,
        ) -> list[tuple[str | None, tuple[Node, ...]]]:
            if owner in active or len(active) >= 16:
                return [(None, ())]
            if known[owner]:
                return [("", ())]
            result: list[tuple[str | None, tuple[Node, ...]]] = []
            for parent, prefix, mount in parents.get(owner, []):
                for base, includes in prefixes(parent, active | {owner}):
                    if len(result) >= 16:
                        return [(None, ())]
                    result.append(
                        (
                            base.rstrip("/") + "/" + prefix.lstrip("/")
                            if base is not None and prefix is not None
                            else None,
                            (*includes, mount),
                        )
                    )
            return result or [(None, ())]

        binding = binding_for(unit.path, config)
        for owner, verb, path, node in registrations:
            for prefix, includes in prefixes(owner)[:16]:
                combined = (
                    prefix.rstrip("/") + "/" + path.lstrip("/")
                    if prefix is not None and path is not None
                    else None
                )
                if combined is None:
                    diagnostics.append(
                        diagnostic("dynamic_express_path", location(unit, index, node))
                    )
                routes.append(
                    RouteObservationV1(
                        key=key(
                            unit,
                            "route",
                            node.start_byte,
                            verb,
                            combined,
                            tuple(n.start_byte for n in includes),
                        ),
                        framework="express",
                        service_key=binding.service_key if binding else None,
                        method="ANY" if verb == "all" else method(verb),
                        raw_path=path,
                        template_path=combined,
                        registration=location(unit, index, node),
                        includes=tuple(location(unit, index, n) for n in includes),
                        state="resolved" if combined is not None else "unresolved",
                        limitations=("middleware_order_unverified",),
                    )
                )
    return tuple(routes), tuple(diagnostics)
