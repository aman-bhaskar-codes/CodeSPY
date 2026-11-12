"""Tree-sitter fact extraction for C and C++ (declarations, types, macros, includes)."""

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

Kind = Literal[
    "function",
    "method",
    "struct",
    "class",
    "enum",
    "type_alias",
    "constant",
    "variable",
    "field",
    "namespace",
]
IDENTIFIERS = {"identifier", "field_identifier", "type_identifier", "namespace_identifier"}
POINTERS = {"pointer_declarator", "reference_declarator", "abstract_pointer_declarator"}
QUALIFIED = {"qualified_identifier", "destructor_name", "operator_name", "operator_cast"}
MODIFIER_NODES = {
    "storage_class_specifier",
    "type_qualifier",
    "virtual",
    "explicit_function_specifier",
    "virtual_specifier",
    "constexpr",
}
HEADERS = (".h", ".hpp", ".hh", ".hxx")
AGGREGATES = {
    "struct_specifier": "struct",
    "union_specifier": "struct",
    "class_specifier": "class",
}


def _drill(node: Node | None) -> tuple[Node | None, Node | None, str]:
    """Resolve a declarator to (name node, function_declarator, type suffix such as '*&')."""
    function: Node | None = None
    suffix = ""
    while node is not None:
        kind = node.type
        if kind in IDENTIFIERS or kind in QUALIFIED:
            return node, function, suffix
        if kind == "function_declarator":
            function = function or node
            node = node.child_by_field_name("declarator")
        elif kind in POINTERS:
            if kind == "reference_declarator":
                suffix += "&&" if node.text and node.text.startswith(b"&&") else "&"
            else:
                suffix += "*"
            node = node.child_by_field_name("declarator") or (
                node.named_children[0] if node.named_children else None
            )
        elif kind == "parenthesized_declarator":
            node = node.named_children[0] if node.named_children else None
        elif kind in {"array_declarator", "init_declarator", "attributed_declarator"}:
            suffix += "[]" if kind == "array_declarator" else ""
            node = node.child_by_field_name("declarator")
        else:
            return None, function, suffix
    return None, function, suffix


def extract(unit: SourceUnitV1, budget: AnalysisBudget) -> FileFactsV1:
    data = unit.data
    index = SourceIndex(data)
    language = unit.language
    grammar_name = language
    tree = parse_bytes(language, data)
    errors = count_errors(tree.root_node, budget)
    if language == "c" and errors and unit.path.endswith(".h"):
        # ".h" is ambiguous: admission calls it C, but many headers are C++. Keep whichever
        # grammar reads the file with fewer errors; the fact language stays the admitted one.
        alternative = parse_bytes("cpp", data)
        alternative_errors = count_errors(alternative.root_node, budget)
        if alternative_errors < errors:
            tree, errors, grammar_name = alternative, alternative_errors, "cpp"
    is_header = unit.path.endswith(HEADERS)
    suffix = "." + unit.path.rsplit(".", 1)[-1] if "." in unit.path.rsplit("/", 1)[-1] else ""
    module = unit.path.removesuffix(suffix).replace("/", ".")

    symbols: list[SymbolFactV1] = []
    imports: list[ImportFactV1] = []
    exports: list[ExportFactV1] = []
    truncated = False
    in_template = False

    def span(node: Node) -> SourceRangeV1:
        return index.range_from_bytes(node.start_byte, node.end_byte)

    def room() -> bool:
        nonlocal truncated
        if len(symbols) >= MAX_FILE_FACTS or len(imports) >= MAX_FILE_FACTS:
            truncated = True
            return False
        return True

    def modifiers_of(node: Node) -> tuple[str, ...]:
        found = [text(data, c) for c in node.children if c.type in MODIFIER_NODES]
        found += [text(data, c) for c in node.children if c.type == "type_qualifier"]
        return tuple(dict.fromkeys(m.strip() for m in found if m.strip()))[:16]

    def trailing(function: Node | None) -> tuple[str, ...]:
        if function is None:
            return ()
        kinds = {"type_qualifier", "virtual_specifier", "noexcept", "trailing_return_type"}
        return tuple(text(data, c).strip() for c in function.children if c.type in kinds)[:8]

    def type_text(node: Node, suffix: str) -> str | None:
        base = clip(text(data, node.child_by_field_name("type")), 480)
        if not base:
            return None
        return base + suffix

    def parameters(function: Node | None) -> tuple[ParameterV1, ...]:
        plist = function.child_by_field_name("parameters") if function is not None else None
        if plist is None:
            return ()
        params: list[ParameterV1] = []
        for child in plist.named_children:
            if child.type in {"parameter_declaration", "optional_parameter_declaration"}:
                name_node, _, suffix = _drill(child.child_by_field_name("declarator"))
                type_str = type_text(child, suffix)
                if type_str == "void" and name_node is None:
                    continue  # f(void)
                default = child.child_by_field_name("default_value")
                params.append(
                    ParameterV1(
                        name=clip(text(data, name_node), 256) or "_",
                        type=type_str,
                        default=clip(text(data, default), 512) if default is not None else None,
                    )
                )
            elif child.type == "variadic_parameter" or text(data, child) == "...":
                params.append(ParameterV1(name="...", kind="variadic"))
            if len(params) >= 128:
                break
        return tuple(params)

    def add(
        kind: Kind,
        name: str,
        container: str,
        node: Node,
        *,
        visibility: str | None = None,
        modifiers: tuple[str, ...] = (),
        signature: SignatureV1 | None = None,
        bases: tuple[str, ...] = (),
        top_level: bool = False,
    ) -> None:
        if not name or not room():
            return
        nonlocal in_template
        if in_template:
            modifiers = ("template", *modifiers)
            in_template = False
        static = "static" in modifiers
        vis = visibility or ("internal" if static else "public")
        public_api = is_header or (kind in {"function", "variable"} and "macro" not in modifiers)
        exported = (
            top_level
            and vis == "public"
            and kind != "field"
            and public_api
            and ("prototype" not in modifiers or is_header)
        )
        symbols.append(
            SymbolFactV1(
                kind=kind,
                name=clip(name, 512),
                qualified_name=clip(f"{container}.{name}", 2048),
                range=span(node),
                visibility=vis,  # type: ignore[arg-type]
                modifiers=modifiers,
                signature=signature,
                bases=bases[:32],
                exported=bool(exported),
                container=None if top_level else clip(container, 2048),
            )
        )
        if exported and len(exports) < MAX_FILE_FACTS:
            exports.append(ExportFactV1(name=clip(name, 512), kind="declared", range=span(node)))

    def aggregate(
        node: Node,
        container: str,
        *,
        top_level: bool,
        fallback: str | None = None,
        extra: tuple[str, ...] = (),
    ) -> str | None:
        """Record struct/union/class/enum with a body; return its name."""
        name_node = node.child_by_field_name("name")
        name = text(data, name_node) or fallback or ""
        body = node.child_by_field_name("body")
        if not name or body is None:
            return name or None
        qualified = f"{container}.{name}"
        if node.type == "enum_specifier":
            add("enum", name, container, node, modifiers=extra, top_level=top_level)
            for entry in body.named_children:
                if entry.type == "enumerator":
                    add(
                        "field",
                        text(data, entry.child_by_field_name("name")),
                        qualified,
                        entry,
                        modifiers=("enumerator",),
                        visibility="public",
                    )
            return name
        kind: Kind = AGGREGATES.get(node.type, "struct")  # type: ignore[assignment]
        bases: list[str] = []
        for clause in node.children:
            if clause.type == "base_class_clause":
                bases += [
                    clip(text(data, c), 256)
                    for c in clause.named_children
                    if c.type in {"type_identifier", "qualified_identifier", "template_type"}
                ]
        add(
            kind,
            name,
            container,
            node,
            bases=tuple(bases),
            modifiers=(("union",) if node.type == "union_specifier" else ()) + extra,
            top_level=top_level,
        )
        members(body, qualified, "private" if node.type == "class_specifier" else "public")
        return name

    def member_declaration(member: Node, container: str, access: str) -> None:
        name_node, function, suffix = _drill(member.child_by_field_name("declarator"))
        mods = modifiers_of(member)
        owner = container.rsplit(".", 1)[-1]
        name = text(data, name_node)
        if function is not None:
            if name_node is not None and name_node.type == "destructor_name":
                mods += ("destructor",)
            elif name == owner:
                mods += ("constructor",)
            elif any(c.type in {"virtual_specifier", "override"} for c in member.children):
                mods += ("virtual",)
            add(
                "method",
                name,
                container,
                member,
                visibility=access,
                modifiers=mods
                + trailing(function)
                + (() if member.type == "function_definition" else ("declaration",)),
                signature=SignatureV1(
                    params=parameters(function),
                    return_type=type_text(member, suffix),
                ),
            )
        elif name:
            add(
                "field",
                name,
                container,
                member,
                visibility=access,
                modifiers=mods,
                signature=SignatureV1(return_type=type_text(member, suffix)),
            )

    def members(body: Node, container: str, access: str) -> None:
        for member in body.named_children:
            if not room():
                return
            match member.type:
                case "access_specifier":
                    access = text(data, member).rstrip(":").strip()
                case "field_declaration" | "declaration" | "function_definition":
                    inner = member.child_by_field_name("type")
                    if inner is not None and inner.type in {*AGGREGATES, "enum_specifier"}:
                        aggregate(inner, container, top_level=False)
                    member_declaration(member, container, access)
                case "template_declaration":
                    for child in member.named_children:
                        if child.type in {
                            "field_declaration",
                            "declaration",
                            "function_definition",
                        }:
                            member_declaration(child, container, access)
                case t if t in AGGREGATES or t == "enum_specifier":
                    aggregate(member, container, top_level=False)

    def function_like(
        node: Node, container: str, *, top_level: bool, extra: tuple[str, ...] = ()
    ) -> None:
        name_node, function, suffix = _drill(node.child_by_field_name("declarator"))
        if name_node is None:
            return
        full = text(data, name_node)
        mods = modifiers_of(node) + trailing(function) + extra
        owner = container
        kind: Kind = "function"
        if name_node.type == "qualified_identifier":  # int Foo::run() { ... }
            scopes: list[str] = []
            leaf: Node | None = name_node
            while leaf is not None and leaf.type == "qualified_identifier":
                scopes.append(text(data, leaf.child_by_field_name("scope")).replace("::", "."))
                leaf = leaf.child_by_field_name("name")
            scope = ".".join(p for p in scopes if p)
            owner = f"{container}.{scope}" if scope else container
            full = text(data, leaf)
            kind = "method"
        if function is None:
            return
        if node.type != "function_definition":
            mods += ("prototype",)
        add(
            kind,
            full,
            owner,
            node,
            modifiers=mods,
            signature=SignatureV1(params=parameters(function), return_type=type_text(node, suffix)),
            top_level=top_level and kind == "function",
        )

    def variable(node: Node, container: str, *, top_level: bool) -> None:
        mods = modifiers_of(node)
        declarators = node.children_by_field_name("declarator")
        for declarator in declarators:
            if not declarator.is_named:
                continue
            name_node, function, suffix = _drill(declarator)
            if name_node is None or function is not None:
                continue
            add(
                "constant" if "const" in mods and not suffix.startswith("*") else "variable",
                text(data, name_node),
                container,
                node,
                modifiers=mods,
                signature=SignatureV1(return_type=type_text(node, suffix)),
                top_level=top_level,
            )

    def set_template(value: bool) -> None:
        nonlocal in_template
        in_template = value

    def statements(block: Node, container: str, *, top_level: bool) -> None:
        for stmt in block.named_children:
            if not room():
                return
            statement(stmt, container, top_level=top_level)

    def statement(stmt: Node, container: str, *, top_level: bool) -> None:
        match stmt.type:
            case "preproc_include":
                path_node = stmt.child_by_field_name("path")
                raw = text(data, path_node)
                local = path_node is not None and path_node.type == "string_literal"
                imports.append(
                    ImportFactV1(
                        kind="static",
                        module=clip(raw.strip('<>"'), 2048),
                        relative_level=1 if local else 0,
                        range=span(stmt),
                    )
                )
            case "preproc_def":
                add(
                    "constant",
                    text(data, stmt.child_by_field_name("name")),
                    container,
                    stmt,
                    modifiers=("macro",),
                    top_level=top_level,
                    signature=SignatureV1(
                        return_type=clip(text(data, stmt.child_by_field_name("value")).strip(), 512)
                        or None
                    ),
                )
            case "preproc_function_def":
                plist = stmt.child_by_field_name("parameters")
                add(
                    "function",
                    text(data, stmt.child_by_field_name("name")),
                    container,
                    stmt,
                    modifiers=("macro",),
                    top_level=top_level,
                    signature=SignatureV1(
                        params=tuple(
                            ParameterV1(name=clip(text(data, p), 256))
                            for p in (plist.named_children if plist else [])
                        )
                    ),
                )
            case (
                "preproc_ifdef"
                | "preproc_if"
                | "preproc_else"
                | "preproc_elif"
                | "linkage_specification"
            ):
                scope_body = stmt.child_by_field_name("body") or stmt
                statements(scope_body, container, top_level=top_level)
            case "function_definition":
                function_like(stmt, container, top_level=top_level)
            case "declaration":
                inner = stmt.child_by_field_name("type")
                if inner is not None and inner.type in {*AGGREGATES, "enum_specifier"}:
                    aggregate(inner, container, top_level=top_level)
                declarators = [d for d in stmt.children_by_field_name("declarator") if d.is_named]
                if declarators and _drill(declarators[0])[1] is not None:
                    function_like(stmt, container, top_level=top_level)
                else:
                    variable(stmt, container, top_level=top_level)
            case t if t in AGGREGATES or t == "enum_specifier":
                aggregate(stmt, container, top_level=top_level)
            case "type_definition":
                inner = stmt.child_by_field_name("type")
                declarators = [d for d in stmt.children_by_field_name("declarator") if d.is_named]
                for declarator in declarators:
                    name_node, function_node, suffix = _drill(declarator)
                    alias = text(data, name_node)
                    target = None
                    if inner is not None and inner.type in {*AGGREGATES, "enum_specifier"}:
                        target = aggregate(inner, container, top_level=top_level, fallback=alias)
                    if target == alias:
                        continue  # typedef struct {...} name; the aggregate already carries it
                    described = (
                        clip(text(data, stmt).removeprefix("typedef").strip().rstrip(";"), 512)
                        if function_node is not None
                        else type_text(stmt, suffix)
                    )
                    add(
                        "type_alias",
                        alias,
                        container,
                        stmt,
                        bases=(target,) if target else (),
                        signature=SignatureV1(return_type=None if target else described),
                        top_level=top_level,
                    )
            case "alias_declaration":
                target_text = clip(text(data, stmt.child_by_field_name("type")), 512) or None
                add(
                    "type_alias",
                    text(data, stmt.child_by_field_name("name")),
                    container,
                    stmt,
                    signature=SignatureV1(return_type=target_text),
                    top_level=top_level,
                )
            case "namespace_definition":
                name = text(data, stmt.child_by_field_name("name"))
                add("namespace", name, container, stmt, top_level=top_level)
                body = stmt.child_by_field_name("body")
                if body is not None and name:
                    statements(body, f"{container}.{name}", top_level=False)
            case "template_declaration":
                for child in stmt.named_children:
                    if child.type in {
                        "function_definition",
                        "declaration",
                        "alias_declaration",
                        *AGGREGATES,
                    }:
                        set_template(True)
                        statement(child, container, top_level=top_level)
                        set_template(False)

    statements(tree.root_node, module, top_level=True)
    imports.sort(key=lambda item: item.range.start_byte)
    return FileFactsV1(
        file_id=unit.file_id,
        path=unit.path,
        sha256=unit.sha256,
        language=language,
        grammar=BY_NAME[grammar_name].pin,
        status="partial" if errors else "parsed",
        error_nodes=errors,
        symbols=tuple(symbols),
        imports=tuple(imports),
        exports=tuple(exports[:MAX_FILE_FACTS]),
        diagnostics=file_diagnostics(errors, truncated),
    )
