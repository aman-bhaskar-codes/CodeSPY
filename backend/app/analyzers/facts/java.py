"""Tree-sitter fact extraction for Java (types, members, annotations, imports)."""

import re
from typing import Literal

from app.analyzers.facts.common import clip, count_errors, file_diagnostics, text
from app.analyzers.positions import SourceIndex
from app.analyzers.registry import BY_NAME, parse_bytes
from app.contracts.ast_facts import (
    MAX_FILE_FACTS,
    ExportFactV1,
    FileFactsV1,
    ImportFactV1,
    ImportNameV1,
    ParameterV1,
    SignatureV1,
    SymbolFactV1,
)
from app.contracts.structure import AnalysisBudget, SourceUnitV1
from tree_sitter import Node

Kind = Literal["class", "interface", "enum", "method", "field", "constant"]
TYPE_NODES = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "record_declaration": "class",
    "annotation_type_declaration": "interface",
}
ACCESS = {"public", "private", "protected"}
# Spring "${name:default}" placeholders: the default may be a fallback credential, so it is masked.
PLACEHOLDER_DEFAULT = re.compile(r"\$\{([^}:]+):[^}]*\}")
KEYWORDS = {
    "static",
    "final",
    "abstract",
    "synchronized",
    "native",
    "default",
    "sealed",
    "strictfp",
}


def _type_names(data: bytes, node: Node | None) -> tuple[str, ...]:
    """Names from superclass, super_interfaces (type_list) or extends_interfaces."""
    if node is None:
        return ()
    names: list[str] = []
    for child in node.named_children:
        if child.type == "type_list":
            names += [clip(text(data, c), 256) for c in child.named_children]
        else:
            names.append(clip(text(data, child), 256))
    return tuple(names[:32])


def _parameters(data: bytes, node: Node | None) -> tuple[ParameterV1, ...]:
    if node is None:
        return ()
    params: list[ParameterV1] = []
    for child in node.named_children:
        if child.type == "formal_parameter":
            params.append(
                ParameterV1(
                    name=clip(text(data, child.child_by_field_name("name")), 256) or "_",
                    type=clip(text(data, child.child_by_field_name("type")), 512) or None,
                )
            )
        elif child.type == "spread_parameter":
            declarator = next(
                (c for c in child.named_children if c.type == "variable_declarator"), None
            )
            type_node = next(
                (
                    c
                    for c in child.named_children
                    if c.type not in {"variable_declarator", "modifiers"}
                ),
                None,
            )
            name = declarator.child_by_field_name("name") if declarator is not None else None
            params.append(
                ParameterV1(
                    name=clip(text(data, name or declarator), 256) or "_",
                    type=clip(text(data, type_node), 512) or None,
                    kind="variadic",
                )
            )
        if len(params) >= 128:
            break
    return tuple(params)


def extract(unit: SourceUnitV1, budget: AnalysisBudget) -> FileFactsV1:
    data = unit.data
    index = SourceIndex(data)
    tree = parse_bytes("java", data)
    errors = count_errors(tree.root_node, budget)

    package_node = next(
        (c for c in tree.root_node.named_children if c.type == "package_declaration"), None
    )
    package = (
        text(data, package_node.named_children[0])
        if package_node and package_node.named_children
        else ""
    )
    module = package or unit.path.removesuffix(".java").replace("/", ".")

    symbols: list[SymbolFactV1] = []
    imports: list[ImportFactV1] = []
    exports: list[ExportFactV1] = []
    truncated = False

    def span(node: Node):
        return index.range_from_bytes(node.start_byte, node.end_byte)

    def room() -> bool:
        nonlocal truncated
        if len(symbols) >= MAX_FILE_FACTS or len(imports) >= MAX_FILE_FACTS:
            truncated = True
            return False
        return True

    def modifier_info(node: Node) -> tuple[tuple[str, ...], tuple[str, ...], str | None]:
        """(keyword modifiers, annotations, access modifier) from a declaration's modifiers."""
        block = next((c for c in node.children if c.type == "modifiers"), None)
        keywords: list[str] = []
        annotations: list[str] = []
        access: str | None = None
        for child in block.children if block is not None else []:
            if child.type in {"marker_annotation", "annotation"}:
                annotations.append(
                    clip(PLACEHOLDER_DEFAULT.sub(r"${\1:…}", text(data, child).lstrip("@")), 256)
                )
            elif child.type in ACCESS:
                access = child.type
            elif child.type in KEYWORDS:
                keywords.append(child.type)
        return tuple(keywords[:16]), tuple(annotations[:32]), access

    def add(
        kind: Kind,
        name: str,
        container: str,
        node: Node,
        *,
        access: str | None,
        default_access: str = "internal",
        modifiers: tuple[str, ...] = (),
        decorators: tuple[str, ...] = (),
        signature: SignatureV1 | None = None,
        bases: tuple[str, ...] = (),
        implements: tuple[str, ...] = (),
        top_level: bool = False,
    ) -> None:
        if not name or not room():
            return
        visibility = access or default_access
        exported = top_level and visibility == "public"
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
                bases=bases,
                implements=implements,
                exported=exported,
                container=None if top_level else clip(container, 2048),
            )
        )
        if exported and len(exports) < MAX_FILE_FACTS:
            exports.append(ExportFactV1(name=clip(name, 512), kind="declared", range=span(node)))

    def members(body: Node, container: str, owner: str) -> None:
        default_access = "public" if owner == "interface" else "internal"
        for member in body.named_children:
            if not room():
                return
            keywords, annotations, access = modifier_info(member)
            match member.type:
                case "field_declaration" | "constant_declaration":
                    type_text = clip(text(data, member.child_by_field_name("type")), 512) or None
                    kind: Kind = (
                        "constant"
                        if {"static", "final"} <= set(keywords) or owner == "interface"
                        else "field"
                    )
                    for declarator in member.children_by_field_name("declarator"):
                        add(
                            kind,
                            text(data, declarator.child_by_field_name("name")),
                            container,
                            member,
                            access=access,
                            default_access=default_access,
                            modifiers=keywords,
                            decorators=annotations,
                            signature=SignatureV1(return_type=type_text) if type_text else None,
                        )
                case "method_declaration" | "constructor_declaration":
                    constructor = member.type == "constructor_declaration"
                    return_type = (
                        None
                        if constructor
                        else clip(text(data, member.child_by_field_name("type")), 512)
                    )
                    add(
                        "method",
                        text(data, member.child_by_field_name("name")),
                        container,
                        member,
                        access=access,
                        default_access=default_access,
                        modifiers=(("constructor",) if constructor else ()) + keywords,
                        decorators=annotations,
                        signature=SignatureV1(
                            params=_parameters(data, member.child_by_field_name("parameters")),
                            return_type=return_type or None,
                        ),
                    )
                case "enum_body_declarations":
                    members(member, container, owner)
                case t if t in TYPE_NODES:
                    type_declaration(member, container, top_level=False)

    def type_declaration(node: Node, container: str, *, top_level: bool) -> None:
        keywords, annotations, access = modifier_info(node)
        kind = TYPE_NODES[node.type]
        name = text(data, node.child_by_field_name("name"))
        extra: tuple[str, ...] = ()
        if node.type == "record_declaration":
            extra = ("record",)
        elif node.type == "annotation_type_declaration":
            extra = ("annotation",)
        extends = node.child_by_field_name("superclass")
        super_ifaces = node.child_by_field_name("interfaces")
        extends_ifaces = next((c for c in node.children if c.type == "extends_interfaces"), None)
        add(
            kind,  # type: ignore[arg-type]
            name,
            container,
            node,
            access=access,
            modifiers=extra + keywords,
            decorators=annotations,
            bases=_type_names(data, extends) or _type_names(data, extends_ifaces),
            implements=_type_names(data, super_ifaces),
            top_level=top_level,
        )
        qualified = f"{container}.{name}"
        if node.type == "record_declaration":
            for component in (node.child_by_field_name("parameters") or node).named_children:
                if component.type == "formal_parameter":
                    add(
                        "field",
                        text(data, component.child_by_field_name("name")),
                        qualified,
                        component,
                        access="private",
                        modifiers=("record_component",),
                        signature=SignatureV1(
                            return_type=clip(text(data, component.child_by_field_name("type")), 512)
                            or None
                        ),
                    )
        body = node.child_by_field_name("body")
        if body is None:
            return
        if node.type == "enum_declaration":
            for constant in body.named_children:
                if constant.type == "enum_constant":
                    add(
                        "field",
                        text(data, constant.child_by_field_name("name")),
                        qualified,
                        constant,
                        access="public",
                        modifiers=("enum_constant",),
                    )
        members(body, qualified, "interface" if kind == "interface" else "class")

    for stmt in tree.root_node.named_children:
        if not room():
            break
        if stmt.type == "import_declaration":
            target = next(
                (c for c in stmt.named_children if c.type in {"scoped_identifier", "identifier"}),
                None,
            )
            full = text(data, target)
            is_static = any(c.type == "static" for c in stmt.children)
            wildcard = any(c.type == "asterisk" for c in stmt.named_children)
            if is_static and not wildcard and "." in full:
                owner, member = full.rsplit(".", 1)
                imports.append(
                    ImportFactV1(
                        kind="static",
                        module=clip(owner, 2048),
                        names=(ImportNameV1(name=clip(member, 512)),),
                        range=span(stmt),
                    )
                )
            else:
                imports.append(
                    ImportFactV1(
                        kind="wildcard" if wildcard else "static",
                        module=clip(full, 2048),
                        range=span(stmt),
                    )
                )
        elif stmt.type in TYPE_NODES:
            type_declaration(stmt, module, top_level=True)

    return FileFactsV1(
        file_id=unit.file_id,
        path=unit.path,
        sha256=unit.sha256,
        language="java",
        grammar=BY_NAME["java"].pin,
        status="partial" if errors else "parsed",
        error_nodes=errors,
        symbols=tuple(symbols),
        imports=tuple(imports),
        exports=tuple(exports[:MAX_FILE_FACTS]),
        diagnostics=file_diagnostics(errors, truncated),
    )
