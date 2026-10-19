"""Grammar registry and safe Tree-sitter parser instantiation."""

import tree_sitter_javascript
import tree_sitter_python
import tree_sitter_typescript
from app.contracts.structure import Language as LanguageName
from tree_sitter import Language, Parser, Tree

_LANGUAGES: dict[LanguageName, Language] = {
    "python": Language(tree_sitter_python.language()),
    "javascript": Language(tree_sitter_javascript.language()),
    "jsx": Language(tree_sitter_javascript.language()),
    "typescript": Language(tree_sitter_typescript.language_typescript()),
    "tsx": Language(tree_sitter_typescript.language_tsx()),
}


def get_language(name: LanguageName) -> Language:
    if name not in _LANGUAGES:
        raise ValueError(f"unsupported language: {name}")
    return _LANGUAGES[name]


def create_parser(name: LanguageName) -> Parser:
    lang = get_language(name)
    return Parser(lang)


def parse_bytes(name: LanguageName, data: bytes) -> Tree:
    """Safe byte parsing: never use progress_callback or removed timeout APIs."""
    parser = create_parser(name)
    return parser.parse(data)
