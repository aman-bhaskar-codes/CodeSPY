"""Iterative Tree-sitter fact extraction for Python (signatures, bases, imports, exports)."""

from typing import Literal

from app.analyzers.facts.common import clip, text
from app.analyzers.positions import SourceIndex
from app.analyzers.registry import parse_bytes
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
from tree_sitter import Node

GRAMMAR = "tree-sitter-python"
ENUM_BASES = {"Enum", "IntEnum", "StrEnum", "Flag", "IntFlag", "ReprEnum"}
DYNAMIC_IMPORTS = {"importlib.import_module", "import_module", "__import__"}
CLAUSES = {"elif_clause", "else_clause", "except_clause", "except_group_clause", "finally_clause"}
COMPOUND = {"if_statement", "try_statement", "with_statement"}


def _nested_blocks(stmt: Node) -> list[Node]:
    """Statement blocks of if/try/with, including elif/else/except/finally clauses."""
    found: list[Node] = []
    pending = [stmt]
    while pending:
        node = pending.pop()
        for child in node.named_children:
            if child.type == "block":
                found.append(child)
            elif child.type in CLAUSES:
                pending.append(child)
    return found[::-1]


def _string_content(data: bytes, node: Node | None) -> str | None:
    if node is None or node.type != "string":
        return None
    return "".join(_text(data, c) for c in node.named_children if c.type == "string_content")


ParamKind = Literal["positional", "keyword_only", "variadic", "variadic_keyword"]


_clip = clip
_text = text


def _visibility(name: str) -> str:
    if name.startswith("__") and not name.endswith("__"):
        return "private"
    if name.startswith("_") and not name.startswith("__"):
        return "internal"
    return "public"


def _parameters(data: bytes, node: Node | None) -> tuple[ParameterV1, ...]:
    if node is None:
        return ()
    params: list[ParameterV1] = []
    keyword_only = False
    for child in node.named_children:
        kind: ParamKind = "keyword_only" if keyword_only else "positional"
        match child.type:
            case "identifier":
                params.append(ParameterV1(name=_clip(_text(data, child), 256), kind=kind))
            case "typed_parameter":
                name = next((c for c in child.named_children if c.type == "identifier"), None)
                if name is None:  # *args: T and **kwargs: T wrap a splat pattern.
                    splat = child.named_children[0] if child.named_children else None
                    if splat is not None and splat.type in {
                        "list_splat_pattern",
                        "dictionary_splat_pattern",
                    }:
                        inner = splat.named_children[0] if splat.named_children else None
                        variadic: ParamKind = (
                            "variadic" if splat.type == "list_splat_pattern" else "variadic_keyword"
                        )
                        params.append(
                            ParameterV1(
                                name=_clip(_text(data, inner), 256) or "_",
                                type=_clip(_text(data, child.child_by_field_name("type")), 512),
                                kind=variadic,
                            )
                        )
                        keyword_only = keyword_only or splat.type == "list_splat_pattern"
                    continue
                params.append(
                    ParameterV1(
                        name=_clip(_text(data, name), 256),
                        type=_clip(_text(data, child.child_by_field_name("type")), 512),
                        kind=kind,
                    )
                )
            case "default_parameter" | "typed_default_parameter":
                params.append(
                    ParameterV1(
                        name=_clip(_text(data, child.child_by_field_name("name")), 256),
                        type=_clip(_text(data, child.child_by_field_name("type")), 512) or None,
                        default=_clip(_text(data, child.child_by_field_name("value")), 512),
                        kind=kind,
                    )
                )
            case "list_splat_pattern":
                inner = child.named_children[0] if child.named_children else None
                params.append(
                    ParameterV1(name=_clip(_text(data, inner), 256) or "_", kind="variadic")
                )
                keyword_only = True
            case "dictionary_splat_pattern":
                inner = child.named_children[0] if child.named_children else None
                params.append(
                    ParameterV1(name=_clip(_text(data, inner), 256) or "_", kind="variadic_keyword")
                )
            case "keyword_separator":
                keyword_only = True
    return tuple(params)


def _import_module(data: bytes, node: Node | None) -> tuple[str, int]:
    if node is None:
        return "", 0
    if node.type == "relative_import":
        prefix = next((c for c in node.children if c.type == "import_prefix"), None)
        level = len(_text(data, prefix)) if prefix is not None else 0
        dotted = next((c for c in node.children if c.type == "dotted_name"), None)
        return _text(data, dotted), level
    return _text(data, node), 0


def _import_names(data: bytes, nodes: list[Node]) -> tuple[ImportNameV1, ...]:
    names: list[ImportNameV1] = []
    for node in nodes:
        if node.type == "aliased_import":
            names.append(
                ImportNameV1(
                    name=_clip(_text(data, node.child_by_field_name("name")), 512),
                    alias=_clip(_text(data, node.child_by_field_name("alias")), 512),
                )
            )
        elif node.type == "dotted_name":
            names.append(ImportNameV1(name=_clip(_text(data, node), 512)))
    return tuple(names)


def extract(unit: SourceUnitV1, budget: AnalysisBudget) -> FileFactsV1:
    data = unit.data
    index = SourceIndex(data)
    tree = parse_bytes("python", data)

    # Whole-tree pass: charge the node budget and count syntax errors.
    error_nodes = 0
    imports: list[ImportFactV1] = []
    walk: list[Node] = [tree.root_node]
    while walk:
        node = walk.pop()
        budget.tick_nodes(1)
        if node.is_error or node.is_missing:
            error_nodes += 1
        if node.type == "call" and len(imports) < MAX_FILE_FACTS:
            fn = node.child_by_field_name("function")
            args = node.child_by_field_name("arguments")
            first = args.named_children[0] if args is not None and args.named_children else None
            target = _string_content(data, first)
            if fn is not None and target and _text(data, fn) in DYNAMIC_IMPORTS:
                imports.append(
                    ImportFactV1(
                        kind="dynamic",
                        module=_clip(target, 2048),
                        range=index.range_from_bytes(node.start_byte, node.end_byte),
                    )
                )
        walk.extend(node.children)

    module = unit.path.removesuffix(".py").replace("/", ".")
    symbols: list[SymbolFactV1] = []
    exports: list[ExportFactV1] = []
    all_names: list[tuple[str, Node]] = []
    diagnostics: list[DiagnosticFactV1] = []
    truncated = False

    def room() -> bool:
        nonlocal truncated
        if len(symbols) >= MAX_FILE_FACTS or len(imports) >= MAX_FILE_FACTS:
            truncated = True
            return False
        return True

    # Scope stack: (block node, qualified container name, container kind, under TYPE_CHECKING)
    scopes: list[tuple[Node, str, str, bool]] = [(tree.root_node, module, "module", False)]
    while scopes and room():
        block, container, container_kind, type_checking = scopes.pop()
        for stmt in block.named_children:
            if not room():
                break
            decorators: tuple[str, ...] = ()
            definition = stmt
            if stmt.type == "decorated_definition":
                decorators = tuple(
                    _clip(_text(data, d).removeprefix("@").strip(), 256)
                    for d in stmt.named_children
                    if d.type == "decorator"
                )[:32]
                definition = stmt.child_by_field_name("definition") or stmt
            span = index.range_from_bytes(stmt.start_byte, stmt.end_byte)
            if definition.type == "function_definition":
                if container_kind == "function":
                    continue  # local functions are not part of the module's API surface
                name = _text(data, definition.child_by_field_name("name"))
                qname = f"{container}.{name}"
                is_async = any(c.type == "async" for c in definition.children)
                symbols.append(
                    SymbolFactV1(
                        kind="method" if container_kind == "class" else "function",
                        name=_clip(name, 512),
                        qualified_name=_clip(qname, 2048),
                        range=span,
                        visibility=_visibility(name),  # type: ignore[arg-type]
                        modifiers=("async",) if is_async else (),
                        decorators=decorators,
                        signature=SignatureV1(
                            params=_parameters(data, definition.child_by_field_name("parameters")),
                            return_type=_clip(
                                _text(data, definition.child_by_field_name("return_type")), 512
                            )
                            or None,
                        ),
                        container=_clip(container, 2048) if container_kind != "module" else None,
                    )
                )
            elif definition.type == "class_definition":
                name = _text(data, definition.child_by_field_name("name"))
                qname = f"{container}.{name}"
                supers = definition.child_by_field_name("superclasses")
                bases = tuple(
                    _clip(_text(data, b), 256)
                    for b in (supers.named_children if supers is not None else [])
                    if b.type != "keyword_argument"
                )[:32]
                base_names = {b.rsplit(".", 1)[-1] for b in bases}
                class_kind: Literal["class", "enum", "interface"] = (
                    "enum"
                    if base_names & ENUM_BASES
                    else "interface"
                    if "Protocol" in base_names
                    else "class"
                )
                symbols.append(
                    SymbolFactV1(
                        kind=class_kind,
                        name=_clip(name, 512),
                        qualified_name=_clip(qname, 2048),
                        range=span,
                        visibility=_visibility(name),  # type: ignore[arg-type]
                        decorators=decorators,
                        bases=bases,
                        container=_clip(container, 2048) if container_kind != "module" else None,
                    )
                )
                body = definition.child_by_field_name("body")
                if body is not None:
                    scopes.append((body, qname, "class", False))
            elif stmt.type == "future_import_statement":
                listed = [
                    c for c in stmt.named_children if c.type in {"dotted_name", "aliased_import"}
                ]
                imports.append(
                    ImportFactV1(
                        kind="from",
                        module="__future__",
                        names=_import_names(data, listed)[:256],
                        range=span,
                    )
                )
            elif stmt.type in {"import_statement", "import_from_statement"}:
                if stmt.type == "import_statement":
                    for child in stmt.named_children:
                        names = _import_names(data, [child])
                        if not names:
                            continue
                        imports.append(
                            ImportFactV1(
                                kind="static",
                                module=names[0].name,
                                alias=names[0].alias,
                                type_only=type_checking,
                                range=index.range_from_bytes(child.start_byte, child.end_byte),
                            )
                        )
                else:
                    module_node = stmt.child_by_field_name("module_name")
                    name, level = _import_module(data, module_node)
                    wildcard = any(c.type == "wildcard_import" for c in stmt.named_children)
                    listed = [
                        c
                        for c in stmt.named_children
                        if (module_node is None or c.id != module_node.id)
                        and c.type in {"dotted_name", "aliased_import"}
                    ]
                    imports.append(
                        ImportFactV1(
                            kind="wildcard" if wildcard else "from",
                            module=_clip(name, 2048),
                            relative_level=min(level, 32),
                            names=_import_names(data, listed)[:256],
                            type_only=type_checking,
                            range=span,
                        )
                    )
            elif stmt.type in COMPOUND and container_kind != "function":
                checking = type_checking or (
                    stmt.type == "if_statement"
                    and _text(data, stmt.child_by_field_name("condition")).strip()
                    in {"TYPE_CHECKING", "typing.TYPE_CHECKING"}
                )
                for nested in _nested_blocks(stmt):
                    scopes.append((nested, container, container_kind, checking))
            elif stmt.type == "type_alias_statement":
                alias = next((c for c in stmt.named_children if c.type == "type"), None)
                alias_name = _text(
                    data, alias.named_children[0] if alias and alias.named_children else None
                )
                if alias_name:
                    symbols.append(
                        SymbolFactV1(
                            kind="type_alias",
                            name=_clip(alias_name, 512),
                            qualified_name=_clip(f"{container}.{alias_name}", 2048),
                            range=span,
                            visibility=_visibility(alias_name),  # type: ignore[arg-type]
                            container=_clip(container, 2048)
                            if container_kind != "module"
                            else None,
                        )
                    )
            elif stmt.type == "expression_statement" and container_kind in {"module", "class"}:
                assignment = stmt.named_children[0] if stmt.named_children else None
                if assignment is None or assignment.type != "assignment":
                    continue
                left = assignment.child_by_field_name("left")
                right = assignment.child_by_field_name("right")
                annotation = _clip(_text(data, assignment.child_by_field_name("type")), 512)
                if left is None or left.type != "identifier":
                    continue
                target = _text(data, left)
                if container_kind == "class":
                    symbols.append(
                        SymbolFactV1(
                            kind="field",
                            name=_clip(target, 512),
                            qualified_name=_clip(f"{container}.{target}", 2048),
                            range=span,
                            visibility=_visibility(target),  # type: ignore[arg-type]
                            signature=SignatureV1(return_type=annotation) if annotation else None,
                            container=_clip(container, 2048),
                        )
                    )
                elif annotation.endswith("TypeAlias"):
                    symbols.append(
                        SymbolFactV1(
                            kind="type_alias",
                            name=_clip(target, 512),
                            qualified_name=_clip(f"{container}.{target}", 2048),
                            range=span,
                            visibility=_visibility(target),  # type: ignore[arg-type]
                        )
                    )
                elif target == "__all__" and right is not None and right.type in {"list", "tuple"}:
                    for item in right.named_children:
                        if item.type == "string":
                            literal = _text(data, item).strip("\"'")
                            if literal:
                                all_names.append((_clip(literal, 512), item))
                elif target.isupper() and not target.startswith("_"):
                    symbols.append(
                        SymbolFactV1(
                            kind="constant",
                            name=_clip(target, 512),
                            qualified_name=_clip(f"{container}.{target}", 2048),
                            range=span,
                            visibility="public",
                        )
                    )

    if all_names:
        exports = [
            ExportFactV1(
                name=name,
                kind="all_list",
                range=index.range_from_bytes(node.start_byte, node.end_byte),
            )
            for name, node in all_names[:MAX_FILE_FACTS]
        ]
    else:
        exports = [
            ExportFactV1(name=s.name, kind="declared", range=s.range)
            for s in symbols
            if s.container is None
            and s.kind in {"function", "class", "constant", "enum", "interface", "type_alias"}
            and s.visibility == "public"
        ][:MAX_FILE_FACTS]

    exported_names = {e.name for e in exports}
    symbols = [
        s.model_copy(update={"exported": True})
        if s.container is None and s.name in exported_names and s.kind != "field"
        else s
        for s in symbols
    ]
    imports.sort(key=lambda item: item.range.start_byte)

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
        language="python",
        grammar=GRAMMAR,
        status="partial" if error_nodes else "parsed",
        error_nodes=error_nodes,
        symbols=tuple(symbols),
        imports=tuple(imports),
        exports=tuple(exports),
        diagnostics=tuple(diagnostics),
    )
