"""HTTP client request observation: fetch and axios calls."""

from typing import Literal
from urllib.parse import urlparse

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
    object_field,
    stable_js_name,
    walk,
)
from app.analyzers.positions import SourceIndex
from app.contracts.structure import (
    AnalysisConfigV1,
    DiagnosticV1,
    HttpRequestObservationV1,
    SourceUnitV1,
    SymbolObservationV1,
)

SUPPORTED_LANGUAGES = {"javascript", "jsx", "typescript", "tsx"}
HTTP_VERBS = {"get", "post", "put", "patch", "delete", "head", "options"}


def _origin_state(scheme: str, url_str: str) -> Literal["known", "relative", "unknown"]:
    if scheme in {"http", "https"}:
        return "known"
    if url_str.startswith("/"):
        return "relative"
    return "unknown"


def observe_http(
    units: tuple[SourceUnitV1, ...],
    symbols: tuple[SymbolObservationV1, ...],
    config: AnalysisConfigV1,
) -> tuple[tuple[HttpRequestObservationV1, ...], tuple[DiagnosticV1, ...]]:
    requests: list[HttpRequestObservationV1] = []
    diagnostics: list[DiagnosticV1] = []

    for unit in sorted(units, key=lambda u: u.path):
        if unit.language not in SUPPORTED_LANGUAGES:
            continue
        root, has_error = js_tree(unit, config)
        if root is None or has_error:
            continue

        index = SourceIndex(unit.data)
        axios_aliases = js_imports(root, unit, "axios")
        binding = binding_for(unit.path, config)
        base_limits: list[str] = []
        if binding:
            base_limits.append(f"source_prefix:{binding.source_prefix}")
            base_limits.append(f"service:{binding.service_key}")
            if binding.same_origin_service:
                base_limits.append(f"same_origin:{binding.same_origin_service}")

        for node in walk(root):
            if node.type != "call_expression":
                continue

            fn = node.child_by_field_name("function")
            if not fn:
                continue

            args = js_args(node)
            loc = location(unit, index, node)
            req_key = key(unit, "http_request", node.start_byte)

            # Check for fetch(...)
            if fn.type == "identifier" and js_text(fn, unit) == "fetch":
                if not args or not stable_js_name(
                    root, node, "fetch", unit, allow_module_binding=False
                ):
                    continue
                url_node = args[0]
                options_node = args[1] if len(args) > 1 else None

                req_method = "GET"
                if options_node is not None and (
                    options_node.type != "object"
                    or any(c.type == "spread_element" for c in options_node.named_children)
                ):
                    req_method = "UNKNOWN"
                if options_node and options_node.type == "object":
                    method_val_node = object_field(options_node, "method", unit)
                    if any(
                        js_text(c.child_by_field_name("key"), unit).strip("\"'") == "method"
                        for c in options_node.named_children
                        if c.type == "pair"
                    ):
                        req_method = "UNKNOWN"
                    if method_val_node and method_val_node.type == "string":
                        lit = js_literal(method_val_node, unit)
                        if lit:
                            req_method = lit.upper()

                if url_node.type == "string":
                    url_str = js_literal(url_node, unit)
                    if url_str is not None:
                        parsed = urlparse(url_str)
                        requests.append(
                            HttpRequestObservationV1(
                                key=req_key,
                                origin_state=_origin_state(parsed.scheme, url_str),
                                method=method(req_method),
                                base_url=f"{parsed.scheme}://{parsed.netloc}"
                                if parsed.netloc
                                else None,
                                path=parsed.path if parsed.path else "/",
                                template_segments=tuple(
                                    s for s in (parsed.path or "/").strip("/").split("/") if s
                                ),
                                location=loc,
                                client="fetch",
                                limitations=tuple(base_limits),
                            )
                        )
                    else:
                        diagnostics.append(diagnostic("dynamic_fetch_url", loc))
                        requests.append(
                            HttpRequestObservationV1(
                                key=req_key,
                                origin_state="unknown",
                                method=method(req_method),
                                location=loc,
                                client="fetch",
                                limitations=tuple(base_limits),
                            )
                        )
                else:
                    diagnostics.append(diagnostic("dynamic_fetch_url", loc))
                    requests.append(
                        HttpRequestObservationV1(
                            key=req_key,
                            origin_state="unknown",
                            method=method(req_method),
                            location=loc,
                            client="fetch",
                            limitations=tuple(base_limits),
                        )
                    )

            # Check for axios.<verb>(...) or axios(...)
            elif axios_aliases:
                mem = member(fn, unit)
                if (
                    mem
                    and mem[0] in axios_aliases
                    and mem[1].lower() in HTTP_VERBS
                    and stable_js_name(root, node, mem[0], unit)
                ):
                    if not args:
                        continue
                    url_node = args[0]
                    verb = mem[1].upper()

                    if url_node.type == "string":
                        url_str = js_literal(url_node, unit)
                        if url_str is not None:
                            parsed = urlparse(url_str)
                            requests.append(
                                HttpRequestObservationV1(
                                    key=req_key,
                                    origin_state=_origin_state(parsed.scheme, url_str),
                                    method=method(verb),
                                    base_url=f"{parsed.scheme}://{parsed.netloc}"
                                    if parsed.netloc
                                    else None,
                                    path=parsed.path if parsed.path else "/",
                                    template_segments=tuple(
                                        s for s in (parsed.path or "/").strip("/").split("/") if s
                                    ),
                                    location=loc,
                                    client="axios",
                                    limitations=tuple(base_limits),
                                )
                            )
                        else:
                            diagnostics.append(diagnostic("dynamic_axios_url", loc))
                            requests.append(
                                HttpRequestObservationV1(
                                    key=req_key,
                                    origin_state="unknown",
                                    method=method(verb),
                                    location=loc,
                                    client="axios",
                                    limitations=tuple(base_limits),
                                )
                            )
                    else:
                        diagnostics.append(diagnostic("dynamic_axios_url", loc))
                        requests.append(
                            HttpRequestObservationV1(
                                key=req_key,
                                origin_state="unknown",
                                method=method(verb),
                                location=loc,
                                client="axios",
                                limitations=tuple(base_limits),
                            )
                        )

    return tuple(requests), tuple(diagnostics)
