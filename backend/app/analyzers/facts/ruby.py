"""Tree-sitter fact extraction for Ruby (modules, classes, methods, mixins, requires)."""

from typing import Literal

from app.analyzers.facts.common import clip, count_errors, file_diagnostics, text
from app.analyzers.positions import SourceIndex
from app.analyzers.registry import BY_NAME, parse_bytes
from app.contracts.ast_facts import (
    MAX_FILE_FACTS,
    ExportFactV1,
    FileFactsV1,
    ImportFactV1,
    ParameterV1,
    SignatureV1,
    SymbolFactV1,
)
from app.contracts.structure import AnalysisBudget, SourceUnitV1
from app.domain.source_ranges import SourceRangeV1
from tree_sitter import Node

Kind = Literal["function", "method", "class", "constant", "field", "namespace"]
REQUIRES = {"require", "require_relative", "load"}
VISIBILITY = {"public", "protected", "private"}
ATTRIBUTES = {"attr_accessor": "accessor", "attr_reader": "reader", "attr_writer": "writer"}


def _parameters(data: bytes, node: Node | None) -> tuple[ParameterV1, ...]:
    if node is None:
        return ()
    params: list[ParameterV1] = []
    for child in node.named_children:
        name = clip(text(data, child.child_by_field_name("name")), 256)
        value = child.child_by_field_name("value")
        default = clip(text(data, value), 512) if value is not None else None
        match child.type:
            case "identifier":
                params.append(ParameterV1(name=clip(text(data, child), 256)))
            case "optional_parameter":
                params.append(ParameterV1(name=name, default=default))
            case "splat_parameter":
                params.append(ParameterV1(name=name or "*", kind="variadic"))
            case "hash_splat_parameter":
                params.append(ParameterV1(name=name or "**", kind="variadic_keyword"))
            case "keyword_parameter":
                params.append(ParameterV1(name=name, default=default, kind="keyword_only"))
            case "block_parameter":
                params.append(ParameterV1(name="&" + name, optional=True))
        if len(params) >= 128:
            break
    return tuple(params)


def _string_value(data: bytes, node: Node | None) -> str | None:
    if node is None or node.type != "string":
        return None
    return "".join(text(data, c) for c in node.named_children if c.type == "string_content")


def extract(unit: SourceUnitV1, budget: AnalysisBudget) -> FileFactsV1:
    data = unit.data
    index = SourceIndex(data)
    tree = parse_bytes("ruby", data)
    errors = count_errors(tree.root_node, budget)
    module = unit.path.removesuffix(".rb").replace("/", ".")

    symbols: list[SymbolFactV1] = []
    imports: list[ImportFactV1] = []
    exports: list[ExportFactV1] = []
    truncated = False

    def span(node: Node) -> SourceRangeV1:
        return index.range_from_bytes(node.start_byte, node.end_byte)

    def room() -> bool:
        nonlocal truncated
        if len(symbols) >= MAX_FILE_FACTS or len(imports) >= MAX_FILE_FACTS:
            truncated = True
            return False
        return True

    def add(
        kind: Kind,
        name: str,
        container: str,
        node: Node,
        *,
        visibility: str = "public",
        modifiers: tuple[str, ...] = (),
        decorators: tuple[str, ...] = (),
        signature: SignatureV1 | None = None,
        bases: tuple[str, ...] = (),
        implements: tuple[str, ...] = (),
        top_level: bool = False,
    ) -> None:
        if not name or not room():
            return
        exported = top_level and visibility == "public" and kind != "field"
        symbols.append(
            SymbolFactV1(
                kind=kind,
                name=clip(name, 512),
                qualified_name=clip(f"{container}.{name}", 2048),
                range=span(node),
                visibility=visibility,  # type: ignore[arg-type]
                modifiers=modifiers,
                decorators=decorators,
                signature=signature,
                bases=bases[:32],
                implements=implements[:32],
                exported=exported,
                container=None if top_level else clip(container, 2048),
            )
        )
        if exported and len(exports) < MAX_FILE_FACTS:
            exports.append(ExportFactV1(name=clip(name, 512), kind="declared", range=span(node)))

    def method(node: Node, container: str, visibility: str, *, top_level: bool) -> None:
        name = text(data, node.child_by_field_name("name"))
        singleton = node.type == "singleton_method"
        if name == "initialize":
            visibility = "private"
        add(
            "function" if top_level else "method",
            name,
            container,
            node,
            visibility=visibility,
            modifiers=("singleton",) if singleton else (),
            signature=SignatureV1(params=_parameters(data, node.child_by_field_name("parameters"))),
            top_level=top_level,
        )

    def symbol_names(args: Node | None) -> list[str]:
        if args is None:
            return []
        return [
            clip(text(data, a).lstrip(":"), 256)
            for a in args.named_children
            if a.type == "simple_symbol"
        ]

    def body_of(node: Node) -> Node | None:
        return node.child_by_field_name("body") or next(
            (c for c in node.named_children if c.type == "body_statement"), None
        )

    def type_declaration(node: Node, container: str, *, top_level: bool) -> None:
        name_node = node.child_by_field_name("name")
        name = text(data, name_node).replace("::", ".")
        if node.type == "module":
            add("namespace", name, container, node, top_level=top_level)
            body = body_of(node)
            if body is not None:
                statements(body, f"{container}.{name}", top_level=False)
            return
        superclass = node.child_by_field_name("superclass")
        base = (
            clip(text(data, superclass.named_children[0]), 256)
            if superclass and superclass.named_children
            else ""
        )
        body = body_of(node)
        mixins: list[str] = []
        extended: list[str] = []
        for stmt in body.named_children if body is not None else []:
            if stmt.type != "call":
                continue
            call = text(data, stmt.child_by_field_name("method"))
            args = [
                clip(text(data, a), 256)
                for a in (stmt.child_by_field_name("arguments") or stmt).named_children
            ]
            if call in {"include", "prepend"}:
                mixins += args
            elif call == "extend":
                extended += [f"extend {a}" for a in args]
        add(
            "class",
            name,
            container,
            node,
            bases=(base,) if base else (),
            implements=tuple(mixins),
            decorators=tuple(extended)[:32],
            top_level=top_level,
        )
        if body is not None:
            statements(body, f"{container}.{name}", top_level=False, in_class=True)

    def statements(block: Node, container: str, *, top_level: bool, in_class: bool = False) -> None:
        default = "public"
        overrides: dict[str, str] = {}
        pending: list[tuple[Node, str]] = []  # methods collected for post-hoc visibility
        for stmt in block.named_children:
            if not room():
                return
            match stmt.type:
                case "identifier" if text(data, stmt) in VISIBILITY:
                    default = text(data, stmt)
                case "method" | "singleton_method":
                    pending.append((stmt, default))
                case "module" | "class":
                    type_declaration(stmt, container, top_level=top_level)
                case "assignment":
                    left = stmt.child_by_field_name("left")
                    if left is not None and left.type == "constant":
                        add("constant", text(data, left), container, stmt, top_level=top_level)
                case "call":
                    name = text(data, stmt.child_by_field_name("method"))
                    args = stmt.child_by_field_name("arguments")
                    if name in REQUIRES and not in_class:
                        target = _string_value(
                            data, args.named_children[0] if args and args.named_children else None
                        )
                        if target:
                            imports.append(
                                ImportFactV1(
                                    kind="require",
                                    module=clip(target, 2048),
                                    relative_level=1 if name == "require_relative" else 0,
                                    range=span(stmt),
                                )
                            )
                    elif name in ATTRIBUTES:
                        for attr in symbol_names(args):
                            add(
                                "field",
                                attr,
                                container,
                                stmt,
                                visibility=default,
                                modifiers=(ATTRIBUTES[name],),
                            )
                    elif name in VISIBILITY and args is not None:
                        for target_node in args.named_children:
                            if target_node.type in {"method", "singleton_method"}:
                                pending.append((target_node, name))
                            elif target_node.type == "simple_symbol":
                                overrides[text(data, target_node).lstrip(":")] = name
        for node, visibility in pending:
            visible = overrides.get(text(data, node.child_by_field_name("name")), visibility)
            method(node, container, visible, top_level=top_level)

    statements(tree.root_node, module, top_level=True)
    imports.sort(key=lambda item: item.range.start_byte)
    return FileFactsV1(
        file_id=unit.file_id,
        path=unit.path,
        sha256=unit.sha256,
        language="ruby",
        grammar=BY_NAME["ruby"].pin,
        status="partial" if errors else "parsed",
        error_nodes=errors,
        symbols=tuple(symbols),
        imports=tuple(imports),
        exports=tuple(exports[:MAX_FILE_FACTS]),
        diagnostics=file_diagnostics(errors, truncated),
    )
