"""Tree-sitter fact extraction for PHP (namespaces, classes, traits, enums, functions, use)."""

from typing import Literal

from app.analyzers.facts.common import clip, file_diagnostics, text
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
from app.domain.source_ranges import SourceRangeV1
from tree_sitter import Node

Kind = Literal["function", "method", "class", "interface", "trait", "enum", "constant", "field"]
TYPE_KINDS: dict[str, Kind] = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "trait_declaration": "trait",
    "enum_declaration": "enum",
}
INCLUDES = {
    "include_expression",
    "include_once_expression",
    "require_expression",
    "require_once_expression",
}
ACCESS = {"public", "protected", "private"}
MODIFIER_NODES = {
    "abstract_modifier": "abstract",
    "final_modifier": "final",
    "static_modifier": "static",
    "readonly_modifier": "readonly",
}


def _name(data: bytes, node: Node | None) -> str:
    return text(data, node).lstrip("$")


def _php_ns(value: str) -> str:
    return value.strip("\\").replace("\\", ".")


def _parameters(data: bytes, node: Node | None) -> tuple[ParameterV1, ...]:
    if node is None:
        return ()
    params: list[ParameterV1] = []
    for child in node.named_children:
        if child.type not in {
            "simple_parameter",
            "property_promotion_parameter",
            "variadic_parameter",
        }:
            continue
        default = child.child_by_field_name("default_value")
        type_node = child.child_by_field_name("type")
        type_text = clip(text(data, type_node), 512) or None
        params.append(
            ParameterV1(
                name=clip(_name(data, child.child_by_field_name("name")), 256) or "_",
                type=type_text,
                default=clip(text(data, default), 512) if default is not None else None,
                kind="variadic" if child.type == "variadic_parameter" else "positional",
                optional=type_text is not None and type_text.startswith("?"),
            )
        )
        if len(params) >= 128:
            break
    return tuple(params)


def extract(unit: SourceUnitV1, budget: AnalysisBudget) -> FileFactsV1:
    data = unit.data
    index = SourceIndex(data)
    tree = parse_bytes("php", data)
    suffix = "." + unit.path.rsplit(".", 1)[-1] if "." in unit.path.rsplit("/", 1)[-1] else ""
    module = unit.path.removesuffix(suffix).replace("/", ".")

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

    # Whole-tree pass: node budget, syntax errors and include/require with literal paths.
    errors = 0
    stack: list[Node] = [tree.root_node]
    while stack:
        node = stack.pop()
        budget.tick_nodes(1)
        if node.is_error or node.is_missing:
            errors += 1
        if node.type in INCLUDES and len(imports) < MAX_FILE_FACTS:
            literal = next((c for c in node.named_children if c.type == "string"), None)
            target = "".join(
                text(data, c)
                for c in (literal.named_children if literal else [])
                if c.type == "string_content"
            )
            if target:
                imports.append(
                    ImportFactV1(kind="require", module=clip(target, 2048), range=span(node))
                )
        stack.extend(node.children)

    def modifier_info(node: Node) -> tuple[tuple[str, ...], str | None]:
        flags: list[str] = []
        access: str | None = None
        for child in node.children:
            if child.type in MODIFIER_NODES:
                flags.append(MODIFIER_NODES[child.type])
            elif child.type == "visibility_modifier":
                access = text(data, child).strip()
        return tuple(flags), access if access in ACCESS else None

    def add(
        kind: Kind,
        name: str,
        container: str,
        node: Node,
        *,
        visibility: str = "public",
        modifiers: tuple[str, ...] = (),
        signature: SignatureV1 | None = None,
        bases: tuple[str, ...] = (),
        implements: tuple[str, ...] = (),
        top_level: bool = False,
    ) -> None:
        if not name or not room():
            return
        exported = top_level and kind != "field"
        symbols.append(
            SymbolFactV1(
                kind=kind,
                name=clip(name, 512),
                qualified_name=clip(f"{container}.{name}", 2048),
                range=span(node),
                visibility=visibility,  # type: ignore[arg-type]
                modifiers=modifiers,
                signature=signature,
                bases=bases[:32],
                implements=implements[:32],
                exported=exported,
                container=None if top_level else clip(container, 2048),
            )
        )
        if exported and len(exports) < MAX_FILE_FACTS:
            exports.append(ExportFactV1(name=clip(name, 512), kind="declared", range=span(node)))

    def names_of(clause: Node | None) -> tuple[str, ...]:
        if clause is None:
            return ()
        return tuple(
            clip(text(data, c), 256)
            for c in clause.named_children
            if c.type in {"name", "qualified_name"}
        )[:32]

    def members(body: Node, container: str, owner: Kind) -> None:
        used: list[str] = []
        for member in body.named_children:
            if not room():
                return
            flags, access = modifier_info(member)
            default_access = "public"
            match member.type:
                case "method_declaration":
                    name = text(data, member.child_by_field_name("name"))
                    add(
                        "method",
                        name,
                        container,
                        member,
                        visibility=access or default_access,
                        modifiers=flags,
                        signature=SignatureV1(
                            params=_parameters(data, member.child_by_field_name("parameters")),
                            return_type=clip(
                                text(data, member.child_by_field_name("return_type")), 512
                            )
                            or None,
                        ),
                    )
                    if name == "__construct":
                        promoted = member.child_by_field_name("parameters")
                        for param in promoted.named_children if promoted is not None else []:
                            if param.type == "property_promotion_parameter":
                                visibility = text(
                                    data, param.child_by_field_name("visibility")
                                ).strip()
                                add(
                                    "field",
                                    _name(data, param.child_by_field_name("name")),
                                    container,
                                    param,
                                    visibility=visibility if visibility in ACCESS else "public",
                                    modifiers=("promoted",),
                                    signature=SignatureV1(
                                        return_type=clip(
                                            text(data, param.child_by_field_name("type")), 512
                                        )
                                        or None
                                    ),
                                )
                case "property_declaration":
                    type_text = clip(text(data, member.child_by_field_name("type")), 512) or None
                    for element in member.named_children:
                        if element.type == "property_element":
                            add(
                                "field",
                                _name(data, element.child_by_field_name("name")),
                                container,
                                member,
                                visibility=access or default_access,
                                modifiers=flags,
                                signature=SignatureV1(return_type=type_text) if type_text else None,
                            )
                case "const_declaration":
                    for element in member.named_children:
                        if element.type == "const_element":
                            add(
                                "constant",
                                text(
                                    data,
                                    next(
                                        (c for c in element.named_children if c.type == "name"),
                                        None,
                                    ),
                                ),
                                container,
                                member,
                                visibility=access or default_access,
                            )
                case "use_declaration":
                    used += [
                        clip(text(data, c), 256)
                        for c in member.named_children
                        if c.type in {"name", "qualified_name"}
                    ]
                case "enum_case":
                    add(
                        "field",
                        text(data, member.child_by_field_name("name")),
                        container,
                        member,
                        modifiers=("case",),
                    )
        if used:  # trait composition belongs to the owning type
            index_of = next(
                (
                    i
                    for i, s in enumerate(symbols)
                    if s.qualified_name == container and s.kind == owner
                ),
                None,
            )
            if index_of is not None:
                current = symbols[index_of]
                symbols[index_of] = current.model_copy(
                    update={"implements": (*current.implements, *used)[:32]}
                )

    def type_declaration(node: Node, container: str, *, top_level: bool) -> None:
        kind = TYPE_KINDS[node.type]
        name = text(data, node.child_by_field_name("name"))
        flags, _ = modifier_info(node)
        base_clause = next((c for c in node.children if c.type == "base_clause"), None)
        impl_clause = next((c for c in node.children if c.type == "class_interface_clause"), None)
        backing = next((c for c in node.named_children if c.type == "primitive_type"), None)
        bases = names_of(base_clause) or ((clip(text(data, backing), 64),) if backing else ())
        add(
            kind,
            name,
            container,
            node,
            modifiers=flags,
            bases=bases,
            implements=names_of(impl_clause),
            top_level=top_level,
        )
        body = node.child_by_field_name("body")
        if body is not None:
            members(body, f"{container}.{name}", kind)

    def use_declaration(node: Node) -> None:
        group = node.child_by_field_name("body")
        prefix_node = next((c for c in node.named_children if c.type in {"namespace_name"}), None)
        if group is not None and prefix_node is not None:
            names = []
            for clause in group.named_children:
                if clause.type != "namespace_use_clause":
                    continue
                alias = clause.child_by_field_name("alias")
                names.append(
                    ImportNameV1(
                        name=clip(
                            text(
                                data,
                                next(
                                    (
                                        c
                                        for c in clause.named_children
                                        if c.type in {"name", "qualified_name"}
                                    ),
                                    None,
                                ),
                            ),
                            512,
                        ),
                        alias=clip(text(data, alias), 512) if alias is not None else None,
                    )
                )
            imports.append(
                ImportFactV1(
                    kind="static",
                    module=clip(text(data, prefix_node), 2048),
                    names=tuple(names[:256]),
                    range=span(node),
                )
            )
            return
        for clause in node.named_children:
            if clause.type != "namespace_use_clause":
                continue
            target = next(
                (c for c in clause.named_children if c.type in {"name", "qualified_name"}), None
            )
            alias = clause.child_by_field_name("alias")
            imports.append(
                ImportFactV1(
                    kind="static",
                    module=clip(text(data, target), 2048),
                    alias=clip(text(data, alias), 512) if alias is not None else None,
                    range=span(node),
                )
            )

    namespace = module
    for stmt in tree.root_node.named_children:
        if not room():
            break
        match stmt.type:
            case "namespace_definition":
                declared = text(data, stmt.child_by_field_name("name"))
                if declared:
                    namespace = _php_ns(declared)
            case "namespace_use_declaration":
                use_declaration(stmt)
            case "function_definition":
                add(
                    "function",
                    text(data, stmt.child_by_field_name("name")),
                    namespace,
                    stmt,
                    signature=SignatureV1(
                        params=_parameters(data, stmt.child_by_field_name("parameters")),
                        return_type=clip(text(data, stmt.child_by_field_name("return_type")), 512)
                        or None,
                    ),
                    top_level=True,
                )
            case "const_declaration":
                for element in stmt.named_children:
                    if element.type == "const_element":
                        add(
                            "constant",
                            text(
                                data,
                                next((c for c in element.named_children if c.type == "name"), None),
                            ),
                            namespace,
                            stmt,
                            top_level=True,
                        )
            case t if t in TYPE_KINDS:
                type_declaration(stmt, namespace, top_level=True)

    imports.sort(key=lambda item: item.range.start_byte)
    return FileFactsV1(
        file_id=unit.file_id,
        path=unit.path,
        sha256=unit.sha256,
        language="php",
        grammar=BY_NAME["php"].pin,
        status="partial" if errors else "parsed",
        error_nodes=errors,
        symbols=tuple(symbols),
        imports=tuple(imports),
        exports=tuple(exports[:MAX_FILE_FACTS]),
        diagnostics=file_diagnostics(errors, truncated),
    )
