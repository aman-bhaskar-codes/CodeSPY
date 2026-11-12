"""Tree-sitter fact extraction for Swift (types, extensions, protocols, members, imports)."""

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
from tree_sitter import Node

Kind = Literal[
    "function",
    "method",
    "class",
    "struct",
    "enum",
    "interface",
    "type_alias",
    "constant",
    "variable",
    "field",
]
ACCESS = {
    "public": "public",
    "open": "public",
    "internal": "internal",
    "package": "internal",
    "fileprivate": "private",
    "private": "private",
}
TYPE_KINDS: dict[str, Kind] = {
    "class": "class",
    "struct": "struct",
    "enum": "enum",
    "actor": "class",
}


def _annotation(data: bytes, node: Node | None) -> str | None:
    value = text(data, node).strip()
    value = value[1:].strip() if value.startswith(":") else value
    return clip(value, 512) or None


def _parameters(data: bytes, node: Node) -> tuple[ParameterV1, ...]:
    """Parameters of a function or initializer; defaults arrive as sibling fields."""
    params: list[ParameterV1] = []
    for position, child in enumerate(node.children):
        field = node.field_name_for_child(position)
        if field == "default_value" and params:
            last = params[-1]
            params[-1] = last.model_copy(update={"default": clip(text(data, child), 512)})
        elif child.type == "parameter":
            parts = child.children_by_field_name("name")
            internal = text(data, parts[0]) if parts else ""
            external = text(data, child.child_by_field_name("external_name"))
            label = (
                f"{external} {internal}"
                if external and external not in {"_", internal}
                else internal
            )
            type_node = parts[1] if len(parts) > 1 else None
            variadic = any(c.type == "..." for c in child.children)
            params.append(
                ParameterV1(
                    name=clip(label, 256) or "_",
                    type=clip(text(data, type_node), 512) or None,
                    kind="variadic" if variadic else "positional",
                )
            )
        if len(params) >= 128:
            break
    return tuple(params)


def _return_type(data: bytes, node: Node) -> str | None:
    arrow = next((i for i, c in enumerate(node.children) if c.type == "->"), None)
    if arrow is None:
        return None
    following = [c for c in node.children[arrow + 1 :] if c.is_named]
    return clip(text(data, following[0]), 512) or None if following else None


def extract(unit: SourceUnitV1, budget: AnalysisBudget) -> FileFactsV1:
    data = unit.data
    index = SourceIndex(data)
    tree = parse_bytes("swift", data)
    errors = count_errors(tree.root_node, budget)
    module = unit.path.removesuffix(".swift").replace("/", ".")

    symbols: list[SymbolFactV1] = []
    imports: list[ImportFactV1] = []
    exports: list[ExportFactV1] = []
    conformances: dict[str, list[str]] = {}
    truncated = False

    def span(node: Node):
        return index.range_from_bytes(node.start_byte, node.end_byte)

    def room() -> bool:
        nonlocal truncated
        if len(symbols) >= MAX_FILE_FACTS or len(imports) >= MAX_FILE_FACTS:
            truncated = True
            return False
        return True

    def modifier_info(node: Node) -> tuple[str | None, tuple[str, ...], tuple[str, ...]]:
        block = next((c for c in node.children if c.type == "modifiers"), None)
        access: str | None = None
        flags: list[str] = []
        decorators: list[str] = []
        for child in block.children if block is not None else []:
            value = text(data, child).strip()
            if child.type == "attribute":
                decorators.append(clip(value.lstrip("@"), 256))
            elif child.type == "visibility_modifier":
                access = ACCESS.get(value.split("(")[0].strip())
            elif child.is_named and value:
                flags.append(value)
        return access, tuple(flags[:16]), tuple(decorators[:32])

    def add(
        kind: Kind,
        name: str,
        container: str,
        node: Node,
        *,
        visibility: str | None = None,
        modifiers: tuple[str, ...] = (),
        decorators: tuple[str, ...] = (),
        signature: SignatureV1 | None = None,
        bases: tuple[str, ...] = (),
        implements: tuple[str, ...] = (),
        top_level: bool = False,
    ) -> None:
        if not name or not room():
            return
        vis = visibility or "internal"
        exported = top_level and vis == "public"
        symbols.append(
            SymbolFactV1(
                kind=kind,
                name=clip(name, 512),
                qualified_name=clip(f"{container}.{name}", 2048),
                range=span(node),
                visibility=vis,  # type: ignore[arg-type]
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

    def inherited(node: Node) -> tuple[str, ...]:
        return tuple(
            clip(text(data, spec.child_by_field_name("inherits_from")), 256)
            for spec in node.children
            if spec.type == "inheritance_specifier"
        )[:32]

    def property_info(node: Node) -> tuple[str, str]:
        pattern = node.child_by_field_name("name")
        ident = pattern.child_by_field_name("bound_identifier") if pattern is not None else None
        binding = next((c for c in node.children if c.type == "value_binding_pattern"), None)
        return text(data, ident), text(data, binding).strip()

    def members(body: Node, container: str, owner: str, *, extension: bool) -> None:
        extra = ("extension",) if extension else ()
        for member in body.named_children:
            if not room():
                return
            access, flags, decorators = modifier_info(member)
            default = "public" if owner == "protocol" else None
            match member.type:
                case "property_declaration" | "protocol_property_declaration":
                    name, binding = property_info(member)
                    type_text = _annotation(
                        data,
                        next((c for c in member.children if c.type == "type_annotation"), None),
                    )
                    add(
                        "field",
                        name,
                        container,
                        member,
                        visibility=access or default,
                        modifiers=extra + flags + ((binding,) if binding else ()),
                        decorators=decorators,
                        signature=SignatureV1(return_type=type_text) if type_text else None,
                    )
                case "function_declaration" | "protocol_function_declaration" | "init_declaration":
                    is_init = member.type == "init_declaration"
                    name = "init" if is_init else text(data, member.child_by_field_name("name"))
                    markers = tuple(
                        c.type for c in member.children if c.type in {"async", "throws", "rethrows"}
                    )
                    add(
                        "method",
                        name,
                        container,
                        member,
                        visibility=access or default,
                        modifiers=extra + (("constructor",) if is_init else ()) + flags + markers,
                        decorators=decorators,
                        signature=SignatureV1(
                            params=_parameters(data, member),
                            return_type=None if is_init else _return_type(data, member),
                        ),
                    )
                case "enum_entry":
                    for name_node in member.children_by_field_name("name"):
                        if name_node.type == "simple_identifier":
                            add(
                                "field",
                                text(data, name_node),
                                container,
                                member,
                                visibility="public",
                                modifiers=("case",),
                            )
                case "class_declaration" | "protocol_declaration":
                    declaration(member, container, top_level=False)
                case "typealias_declaration":
                    declaration(member, container, top_level=False)

    def declaration(node: Node, container: str, *, top_level: bool) -> None:
        access, flags, decorators = modifier_info(node)
        match node.type:
            case "class_declaration":
                keyword = text(data, node.child_by_field_name("declaration_kind")).strip()
                name_node = node.child_by_field_name("name")
                inherits = inherited(node)
                body = node.child_by_field_name("body")
                if keyword == "extension":
                    target = clip(text(data, name_node), 256).split("<", 1)[0]
                    if inherits:
                        conformances.setdefault(target, []).extend(inherits)
                    if body is not None:
                        members(body, f"{container}.{target}", "extension", extension=True)
                    return
                name = text(data, name_node)
                kind = TYPE_KINDS.get(keyword, "class")
                add(
                    kind,
                    name,
                    container,
                    node,
                    visibility=access,
                    modifiers=(("actor",) if keyword == "actor" else ()) + flags,
                    decorators=decorators,
                    bases=inherits if kind == "class" and keyword == "class" else (),
                    implements=() if kind == "class" and keyword == "class" else inherits,
                    top_level=top_level,
                )
                if body is not None:
                    members(body, f"{container}.{name}", "type", extension=False)
            case "protocol_declaration":
                name = text(data, node.child_by_field_name("name"))
                add(
                    "interface",
                    name,
                    container,
                    node,
                    visibility=access,
                    modifiers=flags,
                    decorators=decorators,
                    bases=inherited(node),
                    top_level=top_level,
                )
                body = node.child_by_field_name("body")
                if body is not None:
                    members(body, f"{container}.{name}", "protocol", extension=False)
            case "typealias_declaration":
                names = node.children_by_field_name("name")
                aliased = _annotation(data, names[1]) if len(names) > 1 else None
                add(
                    "type_alias",
                    text(data, names[0]) if names else "",
                    container,
                    node,
                    visibility=access,
                    signature=SignatureV1(return_type=aliased) if aliased else None,
                    top_level=top_level,
                )

    for stmt in tree.root_node.named_children:
        if not room():
            break
        match stmt.type:
            case "import_declaration":
                target = next((c for c in stmt.named_children if c.type == "identifier"), None)
                imports.append(
                    ImportFactV1(
                        kind="static", module=clip(text(data, target), 2048), range=span(stmt)
                    )
                )
            case "class_declaration" | "protocol_declaration" | "typealias_declaration":
                declaration(stmt, module, top_level=True)
            case "function_declaration":
                access, flags, decorators = modifier_info(stmt)
                markers = tuple(
                    c.type for c in stmt.children if c.type in {"async", "throws", "rethrows"}
                )
                add(
                    "function",
                    text(data, stmt.child_by_field_name("name")),
                    module,
                    stmt,
                    visibility=access,
                    modifiers=flags + markers,
                    decorators=decorators,
                    signature=SignatureV1(
                        params=_parameters(data, stmt), return_type=_return_type(data, stmt)
                    ),
                    top_level=True,
                )
            case "property_declaration":
                access, flags, decorators = modifier_info(stmt)
                name, binding = property_info(stmt)
                type_text = _annotation(
                    data, next((c for c in stmt.children if c.type == "type_annotation"), None)
                )
                add(
                    "constant" if binding == "let" else "variable",
                    name,
                    module,
                    stmt,
                    visibility=access,
                    modifiers=flags,
                    decorators=decorators,
                    signature=SignatureV1(return_type=type_text) if type_text else None,
                    top_level=True,
                )

    merged: list[SymbolFactV1] = []
    for symbol in symbols:
        extra = conformances.get(symbol.name) if symbol.container is None else None
        if extra and symbol.kind in {"class", "struct", "enum"}:
            symbol = symbol.model_copy(update={"implements": (*symbol.implements, *extra)[:32]})
        merged.append(symbol)
    imports.sort(key=lambda item: item.range.start_byte)
    return FileFactsV1(
        file_id=unit.file_id,
        path=unit.path,
        sha256=unit.sha256,
        language="swift",
        grammar=BY_NAME["swift"].pin,
        status="partial" if errors else "parsed",
        error_nodes=errors,
        symbols=tuple(merged),
        imports=tuple(imports),
        exports=tuple(exports[:MAX_FILE_FACTS]),
        diagnostics=file_diagnostics(errors, truncated),
    )
