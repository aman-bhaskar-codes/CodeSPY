"""Language dispatch for AST fact extraction, followed by framework enrichment."""

import importlib

from app.contracts.ast_facts import FileFactsV1
from app.contracts.structure import AnalysisBudget, SourceUnitV1

JS_FAMILY = frozenset({"javascript", "jsx", "typescript", "tsx"})
EXTRACTORS: dict[str, str] = {
    "python": "app.analyzers.facts.python",
    "javascript": "app.analyzers.facts.javascript",
    "jsx": "app.analyzers.facts.javascript",
    "typescript": "app.analyzers.facts.javascript",
    "tsx": "app.analyzers.facts.javascript",
    "go": "app.analyzers.facts.go",
    "java": "app.analyzers.facts.java",
    "rust": "app.analyzers.facts.rust",
    "c": "app.analyzers.facts.c_family",
    "cpp": "app.analyzers.facts.c_family",
    "ruby": "app.analyzers.facts.ruby",
    "php": "app.analyzers.facts.php",
    "swift": "app.analyzers.facts.swift",
}
FACT_LANGUAGES = frozenset(EXTRACTORS)


def extract_facts(unit: SourceUnitV1, budget: AnalysisBudget) -> FileFactsV1:
    module = EXTRACTORS.get(unit.language)
    if module is None:
        raise ValueError(f"unsupported language: {unit.language}")
    facts: FileFactsV1 = importlib.import_module(module).extract(unit, budget)
    from app.analyzers.facts.frameworks import enrich

    return enrich(unit, facts, budget)
