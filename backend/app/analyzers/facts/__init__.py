"""Language dispatch for AST fact extraction."""

from app.contracts.ast_facts import FileFactsV1
from app.contracts.structure import AnalysisBudget, SourceUnitV1

JS_FAMILY = frozenset({"javascript", "jsx", "typescript", "tsx"})
FACT_LANGUAGES = (
    frozenset({"python", "go", "java", "rust", "c", "cpp", "ruby", "php", "swift"}) | JS_FAMILY
)


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
    if unit.language in {"c", "cpp"}:
        from app.analyzers.facts.c_family import extract as extract_c

        return extract_c(unit, budget)
    if unit.language == "ruby":
        from app.analyzers.facts.ruby import extract as extract_ruby

        return extract_ruby(unit, budget)
    if unit.language == "php":
        from app.analyzers.facts.php import extract as extract_php

        return extract_php(unit, budget)
    if unit.language == "swift":
        from app.analyzers.facts.swift import extract as extract_swift

        return extract_swift(unit, budget)
    raise ValueError(f"unsupported language: {unit.language}")
