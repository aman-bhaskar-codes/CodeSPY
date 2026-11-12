"""Language dispatch for AST fact extraction."""

from app.contracts.ast_facts import FileFactsV1
from app.contracts.structure import AnalysisBudget, SourceUnitV1

JS_FAMILY = frozenset({"javascript", "jsx", "typescript", "tsx"})
FACT_LANGUAGES = frozenset({"python", "go", "java", "rust"}) | JS_FAMILY


def extract_facts(unit: SourceUnitV1, budget: AnalysisBudget) -> FileFactsV1:
    if unit.language == "python":
        from app.analyzers.facts.python import extract

        return extract(unit, budget)
    if unit.language in JS_FAMILY:
        from app.analyzers.facts.javascript import extract as extract_js

        return extract_js(unit, budget)
    if unit.language == "go":
        from app.analyzers.facts.go import extract as extract_go

        return extract_go(unit, budget)
    if unit.language == "java":
        from app.analyzers.facts.java import extract as extract_java

        return extract_java(unit, budget)
    if unit.language == "rust":
        from app.analyzers.facts.rust import extract as extract_rust

        return extract_rust(unit, budget)
    raise ValueError(f"unsupported language: {unit.language}")
