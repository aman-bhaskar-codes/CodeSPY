"""Language dispatch for AST fact extraction."""

from app.contracts.ast_facts import FileFactsV1
from app.contracts.structure import AnalysisBudget, SourceUnitV1

JS_FAMILY = frozenset({"javascript", "jsx", "typescript", "tsx"})
FACT_LANGUAGES = frozenset({"python"}) | JS_FAMILY


def extract_facts(unit: SourceUnitV1, budget: AnalysisBudget) -> FileFactsV1:
    if unit.language == "python":
        from app.analyzers.facts.python import extract

        return extract(unit, budget)
    if unit.language in JS_FAMILY:
        from app.analyzers.facts.javascript import extract as extract_js

        return extract_js(unit, budget)
    raise ValueError(f"unsupported language: {unit.language}")
