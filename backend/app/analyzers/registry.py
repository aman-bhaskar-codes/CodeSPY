"""Language specifications, grammar loading and safe Tree-sitter parser instantiation."""

import importlib
from dataclasses import dataclass
from functools import cache

from app.contracts.structure import Language as LanguageName
from tree_sitter import Language, Parser, Tree


@dataclass(frozen=True)
class LanguageSpec:
    name: LanguageName
    extensions: tuple[str, ...]
    module: str
    factory: str
    pin: str  # distribution name pinned in uv.lock and the analysis image
    structural: bool  # also handled by structural-small-v1


SPECS: tuple[LanguageSpec, ...] = (
    LanguageSpec("python", (".py",), "tree_sitter_python", "language", "tree-sitter-python", True),
    LanguageSpec(
        "javascript",
        (".js", ".mjs", ".cjs"),
        "tree_sitter_javascript",
        "language",
        "tree-sitter-javascript",
        True,
    ),
    LanguageSpec(
        "jsx", (".jsx",), "tree_sitter_javascript", "language", "tree-sitter-javascript", True
    ),
    LanguageSpec(
        "typescript",
        (".ts",),
        "tree_sitter_typescript",
        "language_typescript",
        "tree-sitter-typescript",
        True,
    ),
    LanguageSpec(
        "tsx", (".tsx",), "tree_sitter_typescript", "language_tsx", "tree-sitter-typescript", True
    ),
    LanguageSpec("go", (".go",), "tree_sitter_go", "language", "tree-sitter-go", False),
    LanguageSpec("rust", (".rs",), "tree_sitter_rust", "language", "tree-sitter-rust", False),
    LanguageSpec("java", (".java",), "tree_sitter_java", "language", "tree-sitter-java", False),
    # ".h" is treated as C; C++ headers should use .hpp/.hh/.hxx.
    LanguageSpec("c", (".c", ".h"), "tree_sitter_c", "language", "tree-sitter-c", False),
    LanguageSpec(
        "cpp",
        (".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx"),
        "tree_sitter_cpp",
        "language",
        "tree-sitter-cpp",
        False,
    ),
    LanguageSpec("ruby", (".rb",), "tree_sitter_ruby", "language", "tree-sitter-ruby", False),
    LanguageSpec("php", (".php",), "tree_sitter_php", "language_php", "tree-sitter-php", False),
    LanguageSpec("swift", (".swift",), "tree_sitter_swift", "language", "tree-sitter-swift", False),
)
BY_NAME: dict[str, LanguageSpec] = {spec.name: spec for spec in SPECS}

# Suffix -> language. Structural analysis keeps its original dialects; facts use all of them.
STRUCTURAL_DIALECTS: dict[str, LanguageName] = {
    ext: spec.name for spec in SPECS if spec.structural for ext in spec.extensions
}
FACT_DIALECTS: dict[str, LanguageName] = {
    ext: spec.name for spec in SPECS for ext in spec.extensions
}


@cache
def get_language(name: LanguageName) -> Language:
    spec = BY_NAME.get(name)
    if spec is None:
        raise ValueError(f"unsupported language: {name}")
    module = importlib.import_module(spec.module)  # installed from the hash-locked image only
    return Language(getattr(module, spec.factory)())


def create_parser(name: LanguageName) -> Parser:
    return Parser(get_language(name))


def parse_bytes(name: LanguageName, data: bytes) -> Tree:
    """Safe byte parsing: never use progress_callback or removed timeout APIs."""
    return create_parser(name).parse(data)
