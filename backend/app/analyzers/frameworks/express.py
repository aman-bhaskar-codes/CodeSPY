"""Express local literal registrations; dynamic routes abstain explicitly."""

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

SUPPORTED_LANGUAGES = {"javascript", "jsx", "typescript", "tsx"}
UNSUPPORTED_PATH_CHARS = {"*", "?", "(", ")", "+"}


def _is_unsupported_path_string(val: str) -> bool:
    return any(c in val for c in UNSUPPORTED_PATH_CHARS)


def observe_express(
    units: tuple[SourceUnitV1, ...],
    symbols: tuple[SymbolObservationV1, ...],
    config: AnalysisConfigV1,
) -> tuple[tuple[RouteObservationV1, ...], tuple[DiagnosticV1, ...]]:
    routes: list[RouteObservationV1] = []
    diagnostics: list[DiagnosticV1] = []

    for unit in sorted(units, key=lambda u: u.path):
        if unit.language not in SUPPORTED_LANGUAGES:
            continue
        root, has_error = js_tree(unit, config)
        if root is None:
            diagnostics.append(diagnostic("framework_syntax_budget"))
            continue
        if has_error:
            diagnostics.append(diagnostic("express_parse_unavailable"))
            continue

        index = SourceIndex(unit.data)
        factories = js_imports(root, unit, "express")
        if not factories:
            continue

        # Track app instances created from express()
        apps: set[str] = set()
        for node in walk(root):
            if node.type == "variable_declarator":
                name_node = node.child_by_field_name("name")
                val_node = node.child_by_field_name("value")
                if (
                    name_node
                    and name_node.type == "identifier"
                    and val_node
                    and val_node.type == "call_expression"
                ):
                    fn_node = val_node.child_by_field_name("function")
                    if fn_node and js_text(fn_node, unit) in factories:
                        apps.add(js_text(name_node, unit))
            elif node.type == "assignment_expression":
                left = node.child_by_field_name("left")
                right = node.child_by_field_name("right")
                if left and left.type == "identifier" and right and right.type == "call_expression":
                    fn_node = right.child_by_field_name("function")
                    if fn_node and js_text(fn_node, unit) in factories:
                        apps.add(js_text(left, unit))

        if not apps:
            continue

        binding = binding_for(unit.path, config)
        service_key = binding.service_key if binding else None

        for node in walk(root):
            if node.type != "call_expression":
                continue
            fn = node.child_by_field_name("function")
            mem = member(fn, unit)
            if not mem:
                continue
            obj_name, prop_name = mem
            if obj_name not in apps:
                continue

            verb = prop_name.lower()
            if verb not in {
                "get",
                "post",
                "put",
                "patch",
                "delete",
                "head",
                "options",
                "use",
                "all",
            }:
                continue

            args = js_args(node)
            if not args:
                continue

            path_node = args[0]
            loc = location(unit, index, node)
            route_key = key(unit, "route", node.start_byte, verb)

            if path_node.type == "string":
                path_str = js_literal(path_node, unit)
                if path_str is None or _is_unsupported_path_string(path_str):
                    diagnostics.append(diagnostic("unsupported_express_path", loc))
                    routes.append(
                        RouteObservationV1(
                            key=route_key,
                            framework="express",
                            service_key=service_key,
                            method=method(verb) if verb != "use" else "ANY",
                            raw_path=js_text(path_node, unit),
                            template_path=None,
                            registration=loc,
                            state="unresolved",
                            limitations=("unsupported_path_syntax",),
                        )
                    )
                else:
                    routes.append(
                        RouteObservationV1(
                            key=route_key,
                            framework="express",
                            service_key=service_key,
                            method=method(verb) if verb != "use" else "ANY",
                            raw_path=path_str,
                            template_path=path_str,
                            registration=loc,
                            state="resolved",
                            limitations=("middleware_order_unverified",),
                        )
                    )
            else:
                diagnostics.append(diagnostic("dynamic_express_path", loc))
                routes.append(
                    RouteObservationV1(
                        key=route_key,
                        framework="express",
                        service_key=service_key,
                        method=method(verb) if verb != "use" else "ANY",
                        raw_path=js_text(path_node, unit),
                        template_path=None,
                        registration=loc,
                        state="unresolved",
                        limitations=("dynamic_path_expression",),
                    )
                )

    return tuple(routes), tuple(diagnostics)
