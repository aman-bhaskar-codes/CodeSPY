"""Tree-sitter fact extraction for Rust (items, impls, traits, use declarations)."""

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
from app.domain.source_ranges import SourceRangeV1
from tree_sitter import Node

Kind = Literal[
    "function",
    "method",
    "struct",
    "enum",
    "trait",
    "type_alias",
    "constant",
    "variable",
    "field",
    "namespace",
]
EXPORTABLE = {
    "function",
    "struct",
    "enum",
    "trait",
    "type_alias",
    "constant",
    "variable",
    "namespace",
}


def _base_type(value: str) -> str:
    """Type name of an impl target without references, generics or path prefixes."""
    value = value.strip().lstrip("&").removeprefix("mut ").strip()
    value = value.split("<", 1)[0]
    return value.rsplit("::", 1)[-1].strip()


def _parameters(data: bytes, node: Node | None) -> tuple[ParameterV1, ...]:
    if node is None:
        return ()
    params: list[ParameterV1] = []
    for child in node.named_children:
        if child.type == "self_parameter":
            params.append(ParameterV1(name="self", type=clip(text(data, child), 512)))
        elif child.type == "parameter":
            params.append(
                ParameterV1(
                    name=clip(text(data, child.child_by_field_name("pattern")), 256) or "_",
                    type=clip(text(data, child.child_by_field_name("type")), 512) or None,
                )
            )
        if len(params) >= 128:
            break
    return tuple(params)


def extract(unit: SourceUnitV1, budget: AnalysisBudget) -> FileFactsV1:
    data = unit.data
    index = SourceIndex(data)
    tree = parse_bytes("rust", data)
    errors = count_errors(tree.root_node, budget)
    module = unit.path.removesuffix(".rs").replace("/", ".")

    symbols: list[SymbolFactV1] = []
    imports: list[ImportFactV1] = []
    exports: list[ExportFactV1] = []
    trait_impls: dict[str, list[str]] = {}
    truncated = False

    def span(node: Node) -> SourceRangeV1:
        return index.range_from_bytes(node.start_byte, node.end_byte)

    def room() -> bool:
        nonlocal truncated
        if len(symbols) >= MAX_FILE_FACTS or len(imports) >= MAX_FILE_FACTS:
            truncated = True
            return False
        return True

    def visibility(node: Node) -> str:
        marker = next((c for c in node.children if c.type == "visibility_modifier"), None)
        if marker is None:
            return "private"
        return "public" if text(data, marker).strip() == "pub" else "internal"

    def fn_modifiers(node: Node) -> tuple[str, ...]:
        block = next((c for c in node.children if c.type == "function_modifiers"), None)
        return (
            tuple(
                c.type
                for c in block.children
                if c.is_named or c.type in {"async", "const", "unsafe"}
            )[:16]
            if block
            else ()
        )

    def add(
        kind: Kind,
        name: str,
        container: str,
        node: Node,
        *,
        decorators: tuple[str, ...] = (),
        modifiers: tuple[str, ...] = (),
        signature: SignatureV1 | None = None,
        bases: tuple[str, ...] = (),
        top_level: bool = False,
        force_visibility: str | None = None,
    ) -> None:
        if not name or not room():
            return
        vis = force_visibility or visibility(node)
        exported = top_level and vis == "public" and kind in EXPORTABLE
        symbols.append(
            SymbolFactV1(
                kind=kind,
                name=clip(name, 512),
                qualified_name=clip(f"{container}.{name}", 2048),
                range=span(node),
                visibility=vis,  # type: ignore[arg-type]
                modifiers=modifiers[:16],
                decorators=decorators[:32],
                signature=signature,
                bases=bases[:32],
                exported=exported,
                container=None if top_level else clip(container, 2048),
            )
        )
        if exported and len(exports) < MAX_FILE_FACTS:
            exports.append(ExportFactV1(name=clip(name, 512), kind="declared", range=span(node)))

    def use_declaration(node: Node) -> None:
        argument = node.child_by_field_name("argument")
        if argument is None:
            return
        is_pub = any(c.type == "visibility_modifier" for c in node.children)
        names: list[ImportNameV1] = []
        alias: str | None = None
        kind: Literal["static", "wildcard"] = "static"
        target = text(data, argument)
        match argument.type:
            case "use_as_clause":
                target = text(data, argument.child_by_field_name("path"))
                alias = clip(text(data, argument.child_by_field_name("alias")), 512) or None
            case "use_wildcard":
                kind = "wildcard"
                target = target.removesuffix("*").removesuffix("::")
            case "scoped_use_list":
                target = text(data, argument.child_by_field_name("path"))
                items = argument.child_by_field_name("list")
                for item in items.named_children if items is not None else []:
                    if item.type == "use_as_clause":
                        names.append(
                            ImportNameV1(
                                name=clip(text(data, item.child_by_field_name("path")), 512),
                                alias=clip(text(data, item.child_by_field_name("alias")), 512),
                            )
                        )
                    else:
                        names.append(ImportNameV1(name=clip(text(data, item), 512)))
        imports.append(
            ImportFactV1(
                kind="reexport" if is_pub else kind,
                module=clip(target, 2048),
                names=tuple(names[:256]),
                alias=alias,
                range=span(node),
            )
        )
        if is_pub and len(exports) < MAX_FILE_FACTS:
            exported_name = alias or (
                names[0].name if len(names) == 1 else target.rsplit("::", 1)[-1]
            )
            exports.append(
                ExportFactV1(
                    name=clip(exported_name or "*", 512),
                    kind="reexport",
                    from_module=clip(target, 2048),
                    range=span(node),
                )
            )

    def items(block: Node, container: str, owner: str, top_level: bool) -> None:
        """owner: module | impl | trait_impl | trait — decides methods and default visibility."""
        pending: list[str] = []
        for node in block.named_children:
            if not room():
                return
            if node.type == "attribute_item":
                pending.append(
                    clip(text(data, node).removeprefix("#[").removesuffix("]").strip(), 256)
                )
                continue
            if node.type in {"line_comment", "block_comment", "inner_attribute_item"}:
                continue
            decorators = tuple(pending)
            pending = []
            match node.type:
                case "use_declaration":
                    use_declaration(node)
                case "function_item" | "function_signature_item":
                    add(
                        "method" if owner in {"impl", "trait_impl", "trait"} else "function",
                        text(data, node.child_by_field_name("name")),
                        container,
                        node,
                        decorators=decorators,
                        modifiers=fn_modifiers(node),
                        signature=SignatureV1(
                            params=_parameters(data, node.child_by_field_name("parameters")),
                            return_type=clip(
                                text(data, node.child_by_field_name("return_type")), 512
                            )
                            or None,
                        ),
                        top_level=top_level and owner == "module",
                        force_visibility="public" if owner in {"trait", "trait_impl"} else None,
                    )
                case "struct_item":
                    name = text(data, node.child_by_field_name("name"))
                    add("struct", name, container, node, decorators=decorators, top_level=top_level)
                    body = node.child_by_field_name("body")
                    if body is not None and body.type == "field_declaration_list":
                        for member in body.named_children:
                            if member.type == "field_declaration":
                                add(
                                    "field",
                                    text(data, member.child_by_field_name("name")),
                                    f"{container}.{name}",
                                    member,
                                    signature=SignatureV1(
                                        return_type=clip(
                                            text(data, member.child_by_field_name("type")), 512
                                        )
                                        or None
                                    ),
                                )
                    elif body is not None:  # tuple struct
                        for position, member in enumerate(body.named_children):
                            if member.type not in {"visibility_modifier", "attribute_item"}:
                                add(
                                    "field",
                                    str(position),
                                    f"{container}.{name}",
                                    member,
                                    signature=SignatureV1(
                                        return_type=clip(text(data, member), 512)
                                    ),
                                    force_visibility="public",
                                )
                case "enum_item":
                    name = text(data, node.child_by_field_name("name"))
                    add("enum", name, container, node, decorators=decorators, top_level=top_level)
                    variants = node.child_by_field_name("body")
                    for variant in variants.named_children if variants is not None else []:
                        if variant.type == "enum_variant":
                            add(
                                "field",
                                text(data, variant.child_by_field_name("name")),
                                f"{container}.{name}",
                                variant,
                                modifiers=("variant",),
                                force_visibility="public",
                            )
                case "trait_item":
                    name = text(data, node.child_by_field_name("name"))
                    bounds = node.child_by_field_name("bounds")
                    add(
                        "trait",
                        name,
                        container,
                        node,
                        decorators=decorators,
                        bases=tuple(
                            clip(text(data, b), 256)
                            for b in bounds.named_children
                            if b.type != "lifetime"
                        )
                        if bounds is not None
                        else (),
                        top_level=top_level,
                    )
                    body = node.child_by_field_name("body")
                    if body is not None:
                        items(body, f"{container}.{name}", "trait", False)
                case "impl_item":
                    target = node.child_by_field_name("type")
                    base = (
                        _base_type(text(data, target.child_by_field_name("type") or target))
                        if target
                        else ""
                    )
                    trait = node.child_by_field_name("trait")
                    if trait is not None and base:
                        trait_impls.setdefault(f"{container}.{base}", []).append(
                            clip(text(data, trait), 256)
                        )
                    body = node.child_by_field_name("body")
                    if body is not None and base:
                        items(
                            body,
                            f"{container}.{base}",
                            "trait_impl" if trait is not None else "impl",
                            False,
                        )
                case "const_item":
                    type_text = clip(text(data, node.child_by_field_name("type")), 512) or None
                    add(
                        "constant",
                        text(data, node.child_by_field_name("name")),
                        container,
                        node,
                        decorators=decorators,
                        signature=SignatureV1(return_type=type_text) if type_text else None,
                        top_level=top_level and owner == "module",
                    )
                case "static_item":
                    type_text = clip(text(data, node.child_by_field_name("type")), 512) or None
                    add(
                        "variable",
                        text(data, node.child_by_field_name("name")),
                        container,
                        node,
                        decorators=decorators,
                        modifiers=("mut",)
                        if any(c.type == "mutable_specifier" for c in node.children)
                        else (),
                        signature=SignatureV1(return_type=type_text) if type_text else None,
                        top_level=top_level,
                    )
                case "type_item":
                    target_text = clip(text(data, node.child_by_field_name("type")), 512) or None
                    add(
                        "type_alias",
                        text(data, node.child_by_field_name("name")),
                        container,
                        node,
                        decorators=decorators,
                        signature=SignatureV1(return_type=target_text) if target_text else None,
                        top_level=top_level and owner == "module",
                    )
                case "mod_item":
                    name = text(data, node.child_by_field_name("name"))
                    add(
                        "namespace",
                        name,
                        container,
                        node,
                        decorators=decorators,
                        top_level=top_level,
                    )
                    body = node.child_by_field_name("body")
                    if body is not None:
                        items(body, f"{container}.{name}", "module", False)

    items(tree.root_node, module, "module", True)

    symbols = [
        s.model_copy(update={"implements": tuple(trait_impls[s.qualified_name][:32])})
        if s.kind in {"struct", "enum"} and s.qualified_name in trait_impls
        else s
        for s in symbols
    ]
    imports.sort(key=lambda item: item.range.start_byte)
    return FileFactsV1(
        file_id=unit.file_id,
        path=unit.path,
        sha256=unit.sha256,
        language="rust",
        grammar=BY_NAME["rust"].pin,
        status="partial" if errors else "parsed",
        error_nodes=errors,
        symbols=tuple(symbols),
        imports=tuple(imports),
        exports=tuple(exports[:MAX_FILE_FACTS]),
        diagnostics=file_diagnostics(errors, truncated),
    )
