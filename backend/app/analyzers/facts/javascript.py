"""Tree-sitter fact extraction for JavaScript, JSX, TypeScript and TSX (shared node shapes)."""

from typing import Literal

from app.analyzers.facts.common import clip, text
from app.analyzers.positions import SourceIndex
from app.analyzers.registry import BY_NAME, parse_bytes
from app.contracts.ast_facts import (
    MAX_FILE_FACTS,
    DiagnosticFactV1,
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

SymbolKind = Literal[
    "function",
    "method",
    "class",
    "interface",
    "enum",
    "type_alias",
    "constant",
    "field",
    "namespace",
]
FUNCTION_NODES = {"function_declaration", "generator_function_declaration", "function_signature"}
FUNCTION_VALUES = {"arrow_function", "function_expression", "function", "generator_function"}
CLASS_NODES = {"class_declaration", "abstract_class_declaration"}
NAMESPACE_NODES = {"internal_module", "module"}


def _annotation(data: bytes, node: Node | None) -> str | None:
    """Type text without the leading colon of a type_annotation."""
    if node is None:
        return None
    value = text(data, node).strip()
    value = value[1:].strip() if value.startswith(":") else value
    return clip(value, 512) or None


def _string_value(data: bytes, node: Node | None) -> str | None:
    if node is None or node.type != "string":
        return None
    return "".join(
        text(data, child) for child in node.named_children if child.type == "string_fragment"
    )


def _parameters(data: bytes, node: Node | None) -> tuple[ParameterV1, ...]:
    if node is None:
        return ()
    if node.type == "identifier":  # x => ...
        return (ParameterV1(name=clip(text(data, node), 256)),)
    params: list[ParameterV1] = []
    for child in node.named_children:
        kind: Literal["positional", "variadic"] = "positional"
        optional = child.type == "optional_parameter"
        pattern: Node | None = child
        type_text: str | None = None
        default: str | None = None
        if child.type in {"required_parameter", "optional_parameter"}:
            pattern = child.child_by_field_name("pattern")
            type_text = _annotation(data, child.child_by_field_name("type"))
            value = child.child_by_field_name("value")
            default = clip(text(data, value), 512) if value is not None else None
        elif child.type == "assignment_pattern":
            pattern = child.child_by_field_name("left")
            value = child.child_by_field_name("right")
            default = clip(text(data, value), 512) if value is not None else None
        if pattern is not None and pattern.type == "rest_pattern":
            kind = "variadic"
            inner = next((c for c in pattern.named_children), None)
            pattern = inner
        name = clip(text(data, pattern), 256) or "_"
        params.append(
            ParameterV1(name=name, type=type_text, default=default, kind=kind, optional=optional)
        )
        if len(params) >= 128:
            break
    return tuple(params)


def _signature(data: bytes, node: Node) -> SignatureV1:
    params = node.child_by_field_name("parameters") or node.child_by_field_name("parameter")
    return SignatureV1(
        params=_parameters(data, params),
        return_type=_annotation(data, node.child_by_field_name("return_type")),
    )


def _modifiers(data: bytes, node: Node) -> tuple[str, ...]:
    found: list[str] = []
    for child in node.children:
        match child.type:
            case "static" | "async" | "abstract" | "readonly" | "override" | "declare":
                found.append(child.type)
            case "get":
                found.append("getter")
            case "set":
                found.append("setter")
            case "*":
                found.append("generator")
            case "override_modifier":
                found.append("override")
    return tuple(dict.fromkeys(found))[:16]


def _visibility(data: bytes, node: Node, name: str) -> str | None:
    for child in node.children:
        if child.type == "accessibility_modifier":
            value = text(data, child).strip()
            if value in {"public", "private", "protected"}:
                return value
    return "private" if name.startswith("#") else None


def extract(unit: SourceUnitV1, budget: AnalysisBudget) -> FileFactsV1:
    data = unit.data
    index = SourceIndex(data)
    tree = parse_bytes(unit.language, data)
    suffix = "." + unit.path.rsplit(".", 1)[-1] if "." in unit.path.rsplit("/", 1)[-1] else ""
    module = unit.path.removesuffix(suffix).replace("/", ".")

    # Whole-tree pass: charge the node budget, count syntax errors and find call-based imports.
    error_nodes = 0
    symbols: list[SymbolFactV1] = []
    imports: list[ImportFactV1] = []
    exports: list[ExportFactV1] = []
    truncated = False
    walk: list[Node] = [tree.root_node]
    while walk:
        node = walk.pop()
        budget.tick_nodes(1)
        if node.is_error or node.is_missing:
            error_nodes += 1
        if node.type == "call_expression" and len(imports) < MAX_FILE_FACTS:
            fn = node.child_by_field_name("function")
            args = node.child_by_field_name("arguments")
            first = args.named_children[0] if args is not None and args.named_children else None
            module_name = _string_value(data, first)
            if fn is not None and module_name:
                if fn.type == "import":
                    kind = "dynamic"
                elif fn.type == "identifier" and text(data, fn) == "require":
                    kind = "require"
                else:
                    kind = ""
                if kind:
                    imports.append(
                        ImportFactV1(
                            kind=kind,  # type: ignore[arg-type]
                            module=clip(module_name, 2048),
                            range=index.range_from_bytes(node.start_byte, node.end_byte),
                        )
                    )
        walk.extend(node.children)

    def span(node: Node) -> SourceRangeV1:
        return index.range_from_bytes(node.start_byte, node.end_byte)

    def room() -> bool:
        nonlocal truncated
        if len(symbols) >= MAX_FILE_FACTS or len(imports) >= MAX_FILE_FACTS:
            truncated = True
            return False
        return True

    def add_symbol(
        kind: SymbolKind,
        name: str,
        container: str,
        node: Node,
        *,
        exported: bool,
        default: bool = False,
        modifiers: tuple[str, ...] = (),
        visibility: str | None = None,
        signature: SignatureV1 | None = None,
        bases: tuple[str, ...] = (),
        implements: tuple[str, ...] = (),
        in_module: bool = False,
    ) -> None:
        if not room():
            return
        extra = tuple(
            m for m in (("export",) if exported else ()) + (("default",) if default else ())
        )
        symbols.append(
            SymbolFactV1(
                kind=kind,
                name=clip(name, 512),
                qualified_name=clip(f"{container}.{name}", 2048),
                range=span(node),
                visibility=visibility,  # type: ignore[arg-type]
                modifiers=(extra + modifiers)[:16],
                signature=signature,
                bases=bases,
                implements=implements,
                exported=exported,
                container=None if in_module else clip(container, 2048),
            )
        )

    def heritage(node: Node) -> tuple[tuple[str, ...], tuple[str, ...]]:
        bases: list[str] = []
        implements: list[str] = []
        for child in node.children:
            if child.type != "class_heritage":
                continue
            for clause in child.children:
                if clause.type == "extends_clause":  # TypeScript
                    bases += [
                        clip(text(data, c), 256) for c in clause.children_by_field_name("value")
                    ]
                elif clause.type == "implements_clause":
                    implements += [clip(text(data, c), 256) for c in clause.named_children]
                elif clause.type not in {"extends", ","}:  # JavaScript: extends <expression>
                    bases.append(clip(text(data, clause), 256))
        return tuple(bases[:32]), tuple(implements[:32])

    def members(body: Node, container: str) -> None:
        for member in body.named_children:
            if not room():
                return
            name_node = (
                member.child_by_field_name("name")
                or member.child_by_field_name("property")
                or member.child_by_field_name("member")
            )
            match member.type:
                case "method_definition" | "abstract_method_signature" | "method_signature":
                    name = text(data, name_node)
                    if not name:
                        continue
                    add_symbol(
                        "method",
                        name,
                        container,
                        member,
                        exported=False,
                        modifiers=_modifiers(data, member),
                        visibility=_visibility(data, member, name),
                        signature=_signature(data, member),
                    )
                case "public_field_definition" | "field_definition" | "property_signature":
                    name = text(data, name_node)
                    if not name:
                        continue
                    type_text = _annotation(data, member.child_by_field_name("type"))
                    add_symbol(
                        "field",
                        name,
                        container,
                        member,
                        exported=False,
                        modifiers=_modifiers(data, member),
                        visibility=_visibility(data, member, name),
                        signature=SignatureV1(return_type=type_text) if type_text else None,
                    )

    def declaration(
        node: Node, container: str, ckind: str, *, exported: bool, default: bool
    ) -> None:
        in_module = ckind == "module"
        if node.type in FUNCTION_NODES:
            name = text(data, node.child_by_field_name("name")) or "default"
            add_symbol(
                "function",
                name,
                container,
                node,
                exported=exported,
                default=default,
                modifiers=_modifiers(data, node),
                signature=_signature(data, node),
                in_module=in_module,
            )
            return
        if node.type in CLASS_NODES or (node.type == "class" and default):
            name = text(data, node.child_by_field_name("name")) or "default"
            bases, implements = heritage(node)
            add_symbol(
                "class",
                name,
                container,
                node,
                exported=exported,
                default=default,
                modifiers=_modifiers(data, node),
                bases=bases,
                implements=implements,
                in_module=in_module,
            )
            body = node.child_by_field_name("body")
            if body is not None:
                members(body, f"{container}.{name}")
            return
        if node.type == "interface_declaration":
            name = text(data, node.child_by_field_name("name"))
            bases = tuple(
                clip(text(data, c), 256)
                for clause in node.children
                if clause.type == "extends_type_clause"
                for c in clause.children_by_field_name("type")
            )[:32]
            add_symbol(
                "interface",
                name,
                container,
                node,
                exported=exported,
                default=default,
                bases=bases,
                in_module=in_module,
            )
            body = node.child_by_field_name("body")
            if body is not None:
                members(body, f"{container}.{name}")
            return
        if node.type == "type_alias_declaration":
            add_symbol(
                "type_alias",
                text(data, node.child_by_field_name("name")),
                container,
                node,
                exported=exported,
                default=default,
                in_module=in_module,
            )
            return
        if node.type == "enum_declaration":
            name = text(data, node.child_by_field_name("name"))
            add_symbol(
                "enum",
                name,
                container,
                node,
                exported=exported,
                default=default,
                modifiers=_modifiers(data, node),
                in_module=in_module,
            )
            body = node.child_by_field_name("body")
            for member in body.named_children if body is not None else []:
                member_name = member.child_by_field_name("name") or member
                add_symbol(
                    "field",
                    text(data, member_name),
                    f"{container}.{name}",
                    member,
                    exported=False,
                )
            return
        if node.type in NAMESPACE_NODES:
            name = text(data, node.child_by_field_name("name"))
            add_symbol(
                "namespace",
                name,
                container,
                node,
                exported=exported,
                default=default,
                in_module=in_module,
            )
            body = node.child_by_field_name("body")
            if body is not None:
                statements(body, f"{container}.{name}", "namespace")
            return
        if node.type in {"lexical_declaration", "variable_declaration"}:
            is_const = any(c.type == "const" for c in node.children)
            for declarator in node.named_children:
                if declarator.type != "variable_declarator" or not room():
                    continue
                name_node = declarator.child_by_field_name("name")
                value = declarator.child_by_field_name("value")
                if name_node is None or name_node.type != "identifier":
                    continue
                name = text(data, name_node)
                if value is not None and value.type in FUNCTION_VALUES:
                    add_symbol(
                        "function",
                        name,
                        container,
                        declarator,
                        exported=exported,
                        default=default,
                        modifiers=_modifiers(data, value),
                        signature=_signature(data, value),
                        in_module=in_module,
                    )
                elif is_const and (exported or name.isupper()):
                    type_text = _annotation(data, declarator.child_by_field_name("type"))
                    add_symbol(
                        "constant",
                        name,
                        container,
                        declarator,
                        exported=exported,
                        default=default,
                        in_module=in_module,
                        signature=SignatureV1(return_type=type_text) if type_text else None,
                    )
            return

    def commonjs(stmt: Node) -> None:
        """module.exports = ..., module.exports.x = ..., exports.x = ..."""
        expression = stmt.named_children[0] if stmt.named_children else None
        if expression is None or expression.type != "assignment_expression":
            return
        left = expression.child_by_field_name("left")
        right = expression.child_by_field_name("right")
        target = text(data, left)
        if target == "module.exports":
            exports.append(ExportFactV1(name="module.exports", kind="default", range=span(stmt)))
            if right is not None and right.type == "object":
                for prop in right.named_children:
                    if prop.type == "shorthand_property_identifier":
                        key = text(data, prop)
                    elif prop.type in {"pair", "method_definition"}:
                        key = text(
                            data,
                            prop.child_by_field_name("key") or prop.child_by_field_name("name"),
                        )
                    else:
                        continue
                    if key and len(exports) < MAX_FILE_FACTS:
                        exports.append(
                            ExportFactV1(name=clip(key, 512), kind="declared", range=span(prop))
                        )
        elif target.startswith(("module.exports.", "exports.")) and target.count(".") == (
            2 if target.startswith("module") else 1
        ):
            exports.append(
                ExportFactV1(
                    name=clip(target.rsplit(".", 1)[-1], 512), kind="declared", range=span(stmt)
                )
            )

    def import_statement(stmt: Node) -> None:
        source = _string_value(data, stmt.child_by_field_name("source"))
        if source is None:
            return
        type_only = any(c.type == "type" for c in stmt.children)
        names: list[ImportNameV1] = []
        alias: str | None = None
        kind: Literal["static", "wildcard"] = "static"
        for clause in stmt.named_children:
            if clause.type != "import_clause":
                continue
            for part in clause.named_children:
                if part.type == "identifier":
                    names.append(ImportNameV1(name="default", alias=clip(text(data, part), 512)))
                elif part.type == "namespace_import":
                    kind = "wildcard"
                    ident = next((c for c in part.named_children if c.type == "identifier"), None)
                    alias = clip(text(data, ident), 512) or None
                elif part.type == "named_imports":
                    for spec in part.named_children:
                        if spec.type != "import_specifier":
                            continue
                        alias_node = spec.child_by_field_name("alias")
                        names.append(
                            ImportNameV1(
                                name=clip(text(data, spec.child_by_field_name("name")), 512),
                                alias=clip(text(data, alias_node), 512) if alias_node else None,
                            )
                        )
        imports.append(
            ImportFactV1(
                kind=kind,
                module=clip(source, 2048),
                names=tuple(names[:256]),
                alias=alias,
                type_only=type_only,
                range=span(stmt),
            )
        )

    def export_statement(stmt: Node, container: str, ckind: str) -> None:
        source = _string_value(data, stmt.child_by_field_name("source"))
        is_default = any(c.type == "default" for c in stmt.children)
        decl = stmt.child_by_field_name("declaration")
        value = stmt.child_by_field_name("value")
        clause = next((c for c in stmt.named_children if c.type == "export_clause"), None)
        star = any(c.type == "*" for c in stmt.children) or any(
            c.type == "namespace_export" for c in stmt.named_children
        )
        if decl is not None:
            declaration(decl, container, ckind, exported=True, default=is_default)
            if ckind == "module":
                name = text(data, decl.child_by_field_name("name"))
                if decl.type in {"lexical_declaration", "variable_declaration"}:
                    for d in decl.named_children:
                        ident = (
                            d.child_by_field_name("name")
                            if d.type == "variable_declarator"
                            else None
                        )
                        if ident is not None and ident.type == "identifier":
                            exports.append(
                                ExportFactV1(
                                    name=text(data, ident), kind="declared", range=span(stmt)
                                )
                            )
                elif name or is_default:
                    exports.append(
                        ExportFactV1(
                            name="default" if is_default else clip(name, 512),
                            kind="default" if is_default else "declared",
                            range=span(stmt),
                        )
                    )
            return
        if value is not None and is_default:
            if value.type == "class":
                declaration(value, container, ckind, exported=True, default=True)
            exports.append(ExportFactV1(name="default", kind="default", range=span(stmt)))
            return
        names: list[ImportNameV1] = []
        if clause is not None:
            for spec in clause.named_children:
                if spec.type != "export_specifier":
                    continue
                alias_node = spec.child_by_field_name("alias")
                local = clip(text(data, spec.child_by_field_name("name")), 512)
                exported = clip(text(data, alias_node), 512) if alias_node else local
                names.append(ImportNameV1(name=local, alias=exported if alias_node else None))
                if len(exports) < MAX_FILE_FACTS:
                    exports.append(
                        ExportFactV1(
                            name=exported,
                            kind="reexport" if source else "declared",
                            from_module=clip(source, 2048) if source else None,
                            range=span(spec),
                        )
                    )
        if source and star and len(exports) < MAX_FILE_FACTS:
            exports.append(
                ExportFactV1(
                    name="*", kind="reexport", from_module=clip(source, 2048), range=span(stmt)
                )
            )
        if source:
            imports.append(
                ImportFactV1(
                    kind="reexport",
                    module=clip(source, 2048),
                    names=tuple(names[:256]),
                    range=span(stmt),
                )
            )

    def statements(block: Node, container: str, ckind: str) -> None:
        for stmt in block.named_children:
            if not room():
                return
            match stmt.type:
                case "import_statement":
                    import_statement(stmt)
                case "export_statement":
                    export_statement(stmt, container, ckind)
                case "ambient_declaration":
                    inner = next((c for c in stmt.named_children), None)
                    if inner is not None:
                        declaration(inner, container, ckind, exported=False, default=False)
                case "expression_statement":
                    inner = stmt.named_children[0] if stmt.named_children else None
                    if inner is not None and inner.type in NAMESPACE_NODES:
                        declaration(inner, container, ckind, exported=False, default=False)
                    elif ckind == "module":
                        commonjs(stmt)
                case _:
                    declaration(stmt, container, ckind, exported=False, default=False)

    statements(tree.root_node, module, "module")
    imports.sort(key=lambda item: item.range.start_byte)
    exported_names = {e.name for e in exports if e.kind == "declared"}
    symbols = [
        sym.model_copy(update={"exported": True})
        if sym.container is None and sym.name in exported_names and not sym.exported
        else sym
        for sym in symbols
    ]

    diagnostics: list[DiagnosticFactV1] = []
    if truncated:
        diagnostics.append(
            DiagnosticFactV1(code="fact_capacity", message="fact capacity reached; file truncated")
        )
    if error_nodes:
        diagnostics.append(
            DiagnosticFactV1(code="syntax_error", message=f"{error_nodes} syntax error nodes")
        )
    return FileFactsV1(
        file_id=unit.file_id,
        path=unit.path,
        sha256=unit.sha256,
        language=unit.language,
        grammar=BY_NAME[unit.language].pin,
        status="partial" if error_nodes else "parsed",
        error_nodes=error_nodes,
        symbols=tuple(symbols),
        imports=tuple(imports),
        exports=tuple(exports[:MAX_FILE_FACTS]),
        diagnostics=tuple(diagnostics),
    )
