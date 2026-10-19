"""Trusted tiny fixtures qualify installed APIs, not a structural analyzer."""

from importlib import import_module
from importlib.metadata import PackageNotFoundError, version

import pytest
from tree_sitter import (
    LANGUAGE_VERSION,
    MIN_COMPATIBLE_LANGUAGE_VERSION,
    Language,
    Parser,
    Query,
    QueryCursor,
)

PINS = {
    "tree-sitter": "0.26.0",
    "tree-sitter-python": "0.25.0",
    "tree-sitter-javascript": "0.25.0",
    "tree-sitter-typescript": "0.23.2",
}


def installed(name):
    try:
        return version(name)
    except PackageNotFoundError:
        pytest.fail(f"required pinned distribution is absent: {name}")


@pytest.mark.parametrize(("name", "expected"), PINS.items())
def test_exact_distribution_pins(name, expected):
    assert installed(name) == expected


CASES = [
    (
        "tree_sitter_python",
        "language",
        b"# \xf0\x9f\x98\x80\r\nasync def caf\xc3\xa9():\r\n    return 1\r\n",
        "(function_definition name: (identifier) @name)",
        "café".encode(),
    ),
    (
        "tree_sitter_javascript",
        "language",
        b"// \xf0\x9f\x98\x80\r\nfunction alpha() { return 1; }\r\n",
        "(function_declaration name: (identifier) @name)",
        b"alpha",
    ),
    (
        "tree_sitter_typescript",
        "language_typescript",
        b"// \xf0\x9f\x98\x80\r\nfunction typed(x: number): number { return x; }\r\n",
        "(function_declaration name: (identifier) @name)",
        b"typed",
    ),
    (
        "tree_sitter_typescript",
        "language_tsx",
        b"// \xf0\x9f\x98\x80\r\nconst view = <div title='hello'/>;\r\n",
        "(jsx_self_closing_element name: (identifier) @name)",
        b"div",
    ),
]


@pytest.mark.parametrize(("module", "factory", "source", "query", "expected"), CASES)
def test_grammar_abi_query_and_original_byte_ranges(module, factory, source, query, expected):
    package = module.replace("_", "-")
    installed(package)
    language = Language(getattr(import_module(module), factory)())
    assert MIN_COMPATIBLE_LANGUAGE_VERSION <= language.abi_version <= LANGUAGE_VERSION
    tree = Parser(language).parse(source)
    assert not tree.root_node.has_error
    assert tree.root_node.end_byte == len(source)
    captures = QueryCursor(Query(language, query)).captures(tree.root_node)
    assert [source[node.start_byte : node.end_byte] for node in captures["name"]] == [expected]
    assert captures["name"][0].start_point.row == 1


def test_invalid_python_remains_visible_and_removed_timeout_is_not_assumed():
    installed("tree-sitter-python")
    parser = Parser(Language(import_module("tree_sitter_python").language()))
    assert not hasattr(parser, "timeout_micros")
    tree = parser.parse(b"def missing(:\n    return 1\n")
    assert tree.root_node.has_error


def test_missing_javascript_token_has_visible_zero_width_diagnostic():
    installed("tree-sitter-javascript")
    parser = Parser(Language(import_module("tree_sitter_javascript").language()))
    tree = parser.parse(b"function example() {")
    assert tree.root_node.has_error
    stack = [tree.root_node]
    missing = []
    while stack:
        node = stack.pop()
        if node.is_missing:
            missing.append(node)
        stack.extend(node.children)
    assert missing and all(node.start_byte == node.end_byte for node in missing)
