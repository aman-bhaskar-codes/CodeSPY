"""Iterative Tree-sitter syntax extraction for TypeScript and TSX."""

from app.analyzers.positions import SourceIndex
from app.analyzers.registry import parse_bytes
from app.contracts.structure import (
    AnalysisBudget,
    DiagnosticV1,
    FileObservationsV1,
    Language,
    SourceLocationV1,
    SourceUnitV1,
    SymbolObservationV1,
)
from app.domain.canonical_json import semantic_digest
from tree_sitter import Node


def extract(unit: SourceUnitV1, budget: AnalysisBudget) -> FileObservationsV1:
    index = SourceIndex(unit.data)
    lang_name: Language = unit.language
    tree = parse_bytes(lang_name, unit.data)
    symbols: list[SymbolObservationV1] = []
    diagnostics: list[DiagnosticV1] = []
    has_errors = False

    mod_range = index.range_from_bytes(0, len(unit.data))
    mod_loc = SourceLocationV1(file_id=unit.file_id, sha256=unit.sha256, range=mod_range)
    mod_qname = unit.path.removesuffix(".ts").removesuffix(".tsx").replace("/", ".")
    mod_key = semantic_digest(
        {
            "version": 1,
            "path": unit.path,
            "file_sha256": unit.sha256,
            "kind": "module",
            "range": mod_range.model_dump(),
            "qualified_name": mod_qname,
            "role": "declaration",
        }
    )
    mod_sym = SymbolObservationV1(
        semantic_key=mod_key,
        kind="module",
        path=unit.path,
        sha256=unit.sha256,
        location=mod_loc,
        name=unit.path.split("/")[-1].split(".")[0],
        qualified_name=mod_qname,
        role="declaration",
    )
    symbols.append(mod_sym)

    stack: list[tuple[Node, str, str]] = [(tree.root_node, mod_qname, mod_key)]

    while stack:
        node, parent_qname, parent_container = stack.pop()
        budget.tick_nodes(1)

        if node.is_error or node.is_missing:
            has_errors = True
            loc = SourceLocationV1(
                file_id=unit.file_id,
                sha256=unit.sha256,
                range=index.range_from_bytes(node.start_byte, node.end_byte),
            )
            diagnostics.append(
                DiagnosticV1(
                    code="syntax_error",
                    severity="error",
                    location=loc,
                    message="syntax error or missing node in typescript source",
                    stage="syntax",
                )
            )

        new_parent_qname = parent_qname
        new_container_key = parent_container

        if node.type in ("class_declaration", "class", "interface_declaration"):
            name_node = node.child_by_field_name("name")
            name = (
                unit.data[name_node.start_byte : name_node.end_byte].decode("utf-8")
                if name_node
                else "AnonymousType"
            )
            qname = f"{parent_qname}.{name}"
            span = index.range_from_bytes(node.start_byte, node.end_byte)
            loc = SourceLocationV1(file_id=unit.file_id, sha256=unit.sha256, range=span)
            is_type_only = node.type == "interface_declaration"
            key = semantic_digest(
                {
                    "version": 1,
                    "path": unit.path,
                    "file_sha256": unit.sha256,
                    "kind": "class",
                    "range": span.model_dump(),
                    "qualified_name": qname,
                    "role": "declaration",
                }
            )
            symbols.append(
                SymbolObservationV1(
                    semantic_key=key,
                    kind="class",
                    path=unit.path,
                    sha256=unit.sha256,
                    location=loc,
                    name=name,
                    qualified_name=qname,
                    container_key=parent_container,
                    role="declaration",
                    type_only=is_type_only,
                )
            )
            new_parent_qname = qname
            new_container_key = key

        elif node.type in ("function_declaration", "function", "generator_function_declaration"):
            name_node = node.child_by_field_name("name")
            name = (
                unit.data[name_node.start_byte : name_node.end_byte].decode("utf-8")
                if name_node
                else f"lambda_{node.start_byte}"
            )
            qname = f"{parent_qname}.{name}"
            span = index.range_from_bytes(node.start_byte, node.end_byte)
            loc = SourceLocationV1(file_id=unit.file_id, sha256=unit.sha256, range=span)
            key = semantic_digest(
                {
                    "version": 1,
                    "path": unit.path,
                    "file_sha256": unit.sha256,
                    "kind": "function",
                    "range": span.model_dump(),
                    "qualified_name": qname,
                    "role": "declaration",
                }
            )
            symbols.append(
                SymbolObservationV1(
                    semantic_key=key,
                    kind="function",
                    path=unit.path,
                    sha256=unit.sha256,
                    location=loc,
                    name=name,
                    qualified_name=qname,
                    container_key=parent_container,
                    role="declaration",
                    is_async=any(c.type == "async" for c in node.children),
                )
            )
            new_parent_qname = qname
            new_container_key = key

        elif node.type == "method_definition":
            name_node = node.child_by_field_name("name")
            name = (
                unit.data[name_node.start_byte : name_node.end_byte].decode("utf-8")
                if name_node
                else "method"
            )
            qname = f"{parent_qname}.{name}"
            span = index.range_from_bytes(node.start_byte, node.end_byte)
            loc = SourceLocationV1(file_id=unit.file_id, sha256=unit.sha256, range=span)
            key = semantic_digest(
                {
                    "version": 1,
                    "path": unit.path,
                    "file_sha256": unit.sha256,
                    "kind": "method",
                    "range": span.model_dump(),
                    "qualified_name": qname,
                    "role": "declaration",
                }
            )
            symbols.append(
                SymbolObservationV1(
                    semantic_key=key,
                    kind="method",
                    path=unit.path,
                    sha256=unit.sha256,
                    location=loc,
                    name=name,
                    qualified_name=qname,
                    container_key=parent_container,
                    role="declaration",
                    is_async=any(c.type == "async" for c in node.children),
                )
            )
            new_parent_qname = qname
            new_container_key = key

        elif node.type == "import_statement":
            span = index.range_from_bytes(node.start_byte, node.end_byte)
            loc = SourceLocationV1(file_id=unit.file_id, sha256=unit.sha256, range=span)
            raw_text = unit.data[node.start_byte : node.end_byte].decode("utf-8")
            is_type_only = any(c.type == "type" for c in node.children)
            key = semantic_digest(
                {
                    "version": 1,
                    "path": unit.path,
                    "file_sha256": unit.sha256,
                    "kind": "import",
                    "range": span.model_dump(),
                    "qualified_name": f"{parent_qname}.import",
                    "role": "import",
                }
            )
            symbols.append(
                SymbolObservationV1(
                    semantic_key=key,
                    kind="import",
                    path=unit.path,
                    sha256=unit.sha256,
                    location=loc,
                    name=raw_text,
                    qualified_name=f"{parent_qname}.import",
                    container_key=parent_container,
                    role="import",
                    type_only=is_type_only,
                )
            )

        elif node.type == "call_expression":
            func_node = node.child_by_field_name("function")
            func_name = (
                unit.data[func_node.start_byte : func_node.end_byte].decode("utf-8")
                if func_node
                else "call"
            )
            span = index.range_from_bytes(node.start_byte, node.end_byte)
            loc = SourceLocationV1(file_id=unit.file_id, sha256=unit.sha256, range=span)
            key = semantic_digest(
                {
                    "version": 1,
                    "path": unit.path,
                    "file_sha256": unit.sha256,
                    "kind": "call",
                    "range": span.model_dump(),
                    "qualified_name": f"{parent_qname}.call.{func_name}",
                    "role": "call",
                }
            )
            symbols.append(
                SymbolObservationV1(
                    semantic_key=key,
                    kind="call",
                    path=unit.path,
                    sha256=unit.sha256,
                    location=loc,
                    name=func_name,
                    qualified_name=f"{parent_qname}.call.{func_name}",
                    container_key=parent_container,
                    role="call",
                )
            )

        for child in reversed(node.children):
            stack.append((child, new_parent_qname, new_container_key))

    return FileObservationsV1(
        file_id=unit.file_id,
        path=unit.path,
        sha256=unit.sha256,
        language=lang_name,
        symbols=tuple(symbols[:200000]),
        diagnostics=tuple(diagnostics[:500]),
        has_syntax_errors=has_errors,
    )
