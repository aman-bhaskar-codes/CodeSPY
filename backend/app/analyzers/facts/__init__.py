"""Language dispatch for AST fact extraction."""

from app.contracts.ast_facts import FileFactsV1
from app.contracts.structure import AnalysisBudget, SourceUnitV1

FACT_LANGUAGES = frozenset({"python"})


def extract_facts(unit: SourceUnitV1, budget: AnalysisBudget) -> FileFactsV1:
    if unit.language == "python":
        from app.analyzers.facts.python import extract

        return extract(unit, budget)
    raise ValueError(f"unsupported language: {unit.language}")
