"""Tree-sitter fact extraction for Go (packages, types, methods, imports, exported names)."""

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
    "function", "method", "struct", "interface", "type_alias", "constant", "variable", "field"
]


def _is_exported(name: str) -> bool:
    return bool(name) and name[0].isupper()


def _base_type(value: str) -> str:
    """Receiver or embedded type name without pointer, package qualifier or type arguments."""
    value = value.lstrip("*").strip()
    value = value.split("[", 1)[0]
    return value


def _string(data: bytes, node: Node | None) -> str:
    if node is None:
        return ""
    return "".join(
        text(data, c) for c in node.named_children if c.type == "interpreted_string_literal_content"
    ) or text(data, node).strip('`"')


def _parameters(data: bytes, node: Node | None) -> tuple[ParameterV1, ...]:
    if node is None:
        return ()
    params: list[ParameterV1] = []
    for child in node.named_children:
        if child.type not in {"parameter_declaration", "variadic_parameter_declaration"}:
            continue
        kind: Literal["positional", "variadic"] = (
            "variadic" if child.type == "variadic_parameter_declaration" else "positional"
        )
        type_text = clip(text(data, child.child_by_field_name("type")), 512) or None
        names = [n for n in child.children_by_field_name("name") if n.is_named]
        if not names:  # unnamed parameter such as (int, error)
            params.append(ParameterV1(name="_", type=type_text, kind=kind))
        for name in names:
            params.append(ParameterV1(name=clip(text(data, name), 256), type=type_text, kind=kind))
        if len(params) >= 128:
            break
    return tuple(params[:128])


def _signature(data: bytes, node: Node) -> SignatureV1:
    result = node.child_by_field_name("result")
    return SignatureV1(
        params=_parameters(data, node.child_by_field_name("parameters")),
        return_type=clip(text(data, result), 512) or None,
    )


def extract(unit: SourceUnitV1, budget: AnalysisBudget) -> FileFactsV1:
    data = unit.data
    index = SourceIndex(data)
    tree = parse_bytes("go", data)
    errors = count_errors(tree.root_node, budget)

    package = next((c for c in tree.root_node.named_children if c.type == "package_clause"), None)
    package_name = (
        text(data, package.named_children[0]) if package and package.named_children else ""
    )
    directory = unit.path.rsplit("/", 1)[0].replace("/", ".") if "/" in unit.path else ""
    module = directory or package_name or unit.path.removesuffix(".go")

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

    def add(
        kind: Kind,
        name: str,
        container: str,
        node: Node,
        *,
        signature: SignatureV1 | None = None,
        bases: tuple[str, ...] = (),
        modifiers: tuple[str, ...] = (),
        top_level: bool = False,
    ) -> None:
        if not name or name == "_" or not room():
            return
        exported = _is_exported(name)
        symbols.append(
            SymbolFactV1(
                kind=kind,
                name=clip(name, 512),
                qualified_name=clip(f"{container}.{name}", 2048),
                range=span(node),
                visibility="public" if exported else "internal",
                modifiers=modifiers,
                signature=signature,
                bases=bases[:32],
                exported=exported and top_level,
                container=None if top_level else clip(container, 2048),
            )
        )
        if exported and top_level and kind != "field" and len(exports) < MAX_FILE_FACTS:
            exports.append(ExportFactV1(name=clip(name, 512), kind="declared", range=span(node)))

    def struct_members(struct: Node, owner: str) -> tuple[str, ...]:
        embedded: list[str] = []
        fields = next(
            (c for c in struct.named_children if c.type == "field_declaration_list"), None
        )
        for member in fields.named_children if fields is not None else []:
            if member.type != "field_declaration":
                continue
            names = [n for n in member.children_by_field_name("name") if n.is_named]
            type_text = clip(text(data, member.child_by_field_name("type")), 512)
            if not names:
                embedded.append(clip(_base_type(type_text), 256))
                continue
            for name in names:
                add(
                    "field",
                    text(data, name),
                    owner,
                    member,
                    signature=SignatureV1(return_type=type_text or None),
                )
        return tuple(embedded)

    def interface_members(iface: Node, owner: str) -> tuple[str, ...]:
        embedded: list[str] = []
        for member in iface.named_children:
            if member.type == "method_elem":
                add(
                    "method",
                    text(data, member.child_by_field_name("name")),
                    owner,
                    member,
                    signature=_signature(data, member),
                )
            elif member.type in {"type_elem", "constraint_elem"}:
                embedded.append(clip(text(data, member), 256))
        return tuple(embedded)

    def type_spec(spec: Node) -> None:
        name = text(data, spec.child_by_field_name("name"))
        underlying = spec.child_by_field_name("type")
        qualified = f"{module}.{name}"
        if spec.type == "type_alias":
            target = clip(text(data, underlying), 512) or None
            add(
                "type_alias",
                name,
                module,
                spec,
                top_level=True,
                signature=SignatureV1(return_type=target),
            )
        elif underlying is not None and underlying.type == "struct_type":
            embedded = struct_members(underlying, qualified)
            add("struct", name, module, spec, bases=embedded, top_level=True)
        elif underlying is not None and underlying.type == "interface_type":
            embedded = interface_members(underlying, qualified)
            add("interface", name, module, spec, bases=embedded, top_level=True)
        else:
            base = clip(text(data, underlying), 256)
            add("type_alias", name, module, spec, bases=(base,) if base else (), top_level=True)

    def value_specs(declaration: Node, kind: Kind) -> None:
        for spec in declaration.named_children:
            if spec.type in {"const_spec", "var_spec"}:
                type_text = clip(text(data, spec.child_by_field_name("type")), 512) or None
                for name in (n for n in spec.children_by_field_name("name") if n.is_named):
                    add(
                        kind,
                        text(data, name),
                        module,
                        spec,
                        top_level=True,
                        signature=SignatureV1(return_type=type_text) if type_text else None,
                    )

    for stmt in tree.root_node.named_children:
        if not room():
            break
        match stmt.type:
            case "import_declaration":
                for spec in [c for c in stmt.named_children if c.type == "import_spec"] + [
                    s
                    for c in stmt.named_children
                    if c.type == "import_spec_list"
                    for s in c.named_children
                    if s.type == "import_spec"
                ]:
                    path = _string(data, spec.child_by_field_name("path"))
                    alias_node = spec.child_by_field_name("name")
                    alias = text(data, alias_node) if alias_node is not None else None
                    wildcard = alias_node is not None and alias_node.type == "dot"
                    imports.append(
                        ImportFactV1(
                            kind="wildcard" if wildcard else "static",
                            module=clip(path, 2048),
                            alias=None if wildcard else (clip(alias, 512) if alias else None),
                            range=span(spec),
                        )
                    )
            case "function_declaration":
                add(
                    "function",
                    text(data, stmt.child_by_field_name("name")),
                    module,
                    stmt,
                    signature=_signature(data, stmt),
                    top_level=True,
                )
            case "method_declaration":
                receiver = stmt.child_by_field_name("receiver")
                decl = (
                    next(
                        (c for c in receiver.named_children if c.type == "parameter_declaration"),
                        None,
                    )
                    if receiver is not None
                    else None
                )
                recv_type = text(data, decl.child_by_field_name("type")) if decl is not None else ""
                owner = f"{module}.{_base_type(recv_type)}" if recv_type else module
                add(
                    "method",
                    text(data, stmt.child_by_field_name("name")),
                    owner,
                    stmt,
                    signature=_signature(data, stmt),
                    modifiers=("pointer_receiver",) if recv_type.startswith("*") else (),
                )
            case "type_declaration":
                for spec in stmt.named_children:
                    if spec.type in {"type_spec", "type_alias"}:
                        type_spec(spec)
            case "const_declaration":
                value_specs(stmt, "constant")
            case "var_declaration":
                value_specs(stmt, "variable")

    imports.sort(key=lambda item: item.range.start_byte)
    return FileFactsV1(
        file_id=unit.file_id,
        path=unit.path,
        sha256=unit.sha256,
        language="go",
        grammar=BY_NAME["go"].pin,
        status="partial" if errors else "parsed",
        error_nodes=errors,
        symbols=tuple(symbols),
        imports=tuple(imports),
        exports=tuple(exports[:MAX_FILE_FACTS]),
        diagnostics=file_diagnostics(errors, truncated),
    )
