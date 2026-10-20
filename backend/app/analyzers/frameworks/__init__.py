"""Pure bounded framework syntax helpers. Source code is never executed."""

import ast
from collections.abc import Iterator
from typing import Literal

import tree_sitter_javascript
import tree_sitter_typescript
from app.analyzers.positions import SourceIndex
from app.contracts.structure import (
    AnalysisConfigV1,
    DiagnosticV1,
    Method,
    ServiceBindingV1,
    SourceLocationV1,
    SourceUnitV1,
    SymbolObservationV1,
)
from app.domain.canonical_json import semantic_digest
from tree_sitter import Language, Node, Parser

METHODS: tuple[Method, ...] = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")


def method(value: str | None) -> Method:
    candidate = (value or "").upper()
    return next((m for m in METHODS if m == candidate), "UNKNOWN")


def binding_for(path: str, config: AnalysisConfigV1) -> ServiceBindingV1 | None:
    matches = [
        b
        for b in config.service_bindings
        if path == b.source_prefix.rstrip("/") or path.startswith(b.source_prefix.rstrip("/") + "/")
    ]
    if not matches:
        return None
    longest = max(len(b.source_prefix) for b in matches)
    selected = [b for b in matches if len(b.source_prefix) == longest]
    return selected[0] if len(selected) == 1 else None


def key(unit: SourceUnitV1, kind: str, start: int, *parts: object) -> str:
    return semantic_digest(
        {"path": unit.path, "sha256": unit.sha256, "kind": kind, "start": start, "parts": parts}
    )


def location(unit: SourceUnitV1, index: SourceIndex, node: Node | ast.AST) -> SourceLocationV1:
    if isinstance(node, Node):
        span = index.range_from_bytes(node.start_byte, node.end_byte)
    else:
        starts = [0]
        for line in unit.data.splitlines(keepends=True):
            starts.append(starts[-1] + len(line))
        lineno = getattr(node, "lineno", 1)
        col_offset = getattr(node, "col_offset", 0)
        end_lineno = getattr(node, "end_lineno", lineno)
        end_col_offset = getattr(node, "end_col_offset", col_offset)
        start_byte = starts[lineno - 1] + col_offset if 0 <= lineno - 1 < len(starts) else 0
        end_byte = (
            starts[end_lineno - 1] + end_col_offset
            if 0 <= end_lineno - 1 < len(starts)
            else start_byte
        )
        span = index.range_from_bytes(start_byte, end_byte)
    return SourceLocationV1(file_id=unit.file_id, sha256=unit.sha256, range=span)


def diagnostic(code: str, where: SourceLocationV1 | None = None) -> DiagnosticV1:
    return DiagnosticV1(
        code=code, location=where, message=code.replace("_", " "), stage="framework"
    )


def handler_symbol(
    symbols: tuple[SymbolObservationV1, ...], unit: SourceUnitV1, name: str | None
) -> SymbolObservationV1 | None:
    matches = [
        s
        for s in symbols
        if s.file_id == unit.file_id and s.kind in ("function", "method") and s.name == name
    ]
    return matches[0] if len(matches) == 1 else None


def literal(node: ast.AST | None) -> str | None:
    return (
        node.value
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and len(node.value) <= 2048
        else None
    )


def js_text(node: Node | None, unit: SourceUnitV1) -> str:
    return unit.data[node.start_byte : node.end_byte].decode() if node else ""


def js_literal(node: Node | None, unit: SourceUnitV1) -> str | None:
    if node is None or node.type != "string":
        return None
    raw = js_text(node, unit)
    # Escape sequences have language-specific interpretation: abstain rather than guess.
    if "\\" in raw or len(raw) > 2050:
        return None
    return raw[1:-1]


def js_tree(unit: SourceUnitV1, config: AnalysisConfigV1) -> tuple[Node | None, bool]:
    grammar = (
        tree_sitter_typescript.language_tsx()
        if unit.language == "tsx"
        else tree_sitter_typescript.language_typescript()
        if unit.language == "typescript"
        else tree_sitter_javascript.language()
    )
    root = Parser(Language(grammar)).parse(unit.data).root_node
    count = 0
    pending = [root]
    while pending:
        current = pending.pop()
        count += 1
        if count > config.limits.syntax_nodes_per_file:
            return None, True
        pending.extend(current.named_children)
    return root, root.has_error


def walk(root: Node) -> Iterator[Node]:
    pending = [root]
    while pending:
        node = pending.pop()
        yield node
        pending.extend(reversed(node.named_children))


def js_args(node: Node) -> tuple[Node, ...]:
    args = node.child_by_field_name("arguments")
    return tuple(args.named_children) if args else ()


def member(node: Node | None, unit: SourceUnitV1) -> tuple[str, str] | None:
    if node is None or node.type != "member_expression":
        return None
    obj, prop = node.child_by_field_name("object"), node.child_by_field_name("property")
    if (
        obj is None
        or obj.type != "identifier"
        or prop is None
        or prop.type != "property_identifier"
    ):
        return None
    return js_text(obj, unit), js_text(prop, unit)


def object_field(node: Node | None, field: str, unit: SourceUnitV1) -> Node | None:
    if node is None or node.type != "object":
        return None
    pairs = [
        p
        for p in node.named_children
        if p.type == "pair" and js_text(p.child_by_field_name("key"), unit).strip("\"'") == field
    ]
    # A spread or duplicate property may overwrite any earlier literal.
    if len(pairs) != 1 or any(p.type == "spread_element" for p in node.named_children):
        return None
    return pairs[0].child_by_field_name("value")


def js_imports(root: Node, unit: SourceUnitV1, package: Literal["express", "axios"]) -> set[str]:
    aliases: set[str] = set()
    for node in root.named_children:
        if (
            node.type == "import_statement"
            and js_literal(node.child_by_field_name("source"), unit) == package
        ):
            clause = next((c for c in node.named_children if c.type == "import_clause"), None)
            if clause:
                aliases.update(
                    js_text(c, unit) for c in clause.named_children if c.type == "identifier"
                )
        for child in walk(node):
            if child.type == "variable_declarator" and module_level(child):
                value = child.child_by_field_name("value")
                name = child.child_by_field_name("name")
                if name is None or name.type != "identifier" or value is None:
                    continue
                args = js_args(value) if value.type == "call_expression" else ()
                if (
                    js_text(value.child_by_field_name("function"), unit) == "require"
                    and len(args) == 1
                    and js_literal(args[0], unit) == package
                ) or (value.type == "identifier" and js_text(value, unit) in aliases):
                    aliases.add(js_text(name, unit))
    return aliases


def module_level(node: Node) -> bool:
    parent = node.parent
    while parent:
        if parent.type in {
            "function_declaration",
            "function_expression",
            "arrow_function",
            "method_definition",
            "class_declaration",
        }:
            return False
        parent = parent.parent
    return True


def stable_js_name(
    root: Node, site: Node, name: str, unit: SourceUnitV1, *, allow_module_binding: bool = True
) -> bool:
    """Conservative identity gate; unsupported shadowing/rebinding abstains."""
    parent = site.parent
    while parent:
        if parent.type in {
            "function_declaration",
            "function_expression",
            "arrow_function",
            "method_definition",
        }:
            parameters = parent.child_by_field_name("parameters") or parent.child_by_field_name(
                "parameter"
            )
            if parameters and any(
                n.type == "identifier" and js_text(n, unit) == name for n in walk(parameters)
            ):
                return False
            if any(
                n.type == "variable_declarator"
                and js_text(n.child_by_field_name("name"), unit) == name
                for n in walk(parent)
            ):
                return False
        parent = parent.parent
    declarations = 0
    for node in walk(root):
        if (
            node.type == "assignment_expression"
            and js_text(node.child_by_field_name("left"), unit) == name
        ):
            return False
        if (
            node.type == "variable_declarator"
            and module_level(node)
            and js_text(node.child_by_field_name("name"), unit) == name
        ):
            declarations += 1
        if (
            not allow_module_binding
            and node.type == "import_statement"
            and any(n.type == "identifier" and js_text(n, unit) == name for n in walk(node))
        ):
            return False
    return declarations <= (1 if allow_module_binding else 0)
