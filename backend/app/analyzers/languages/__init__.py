"""Language syntax extraction dispatch."""

from app.analyzers.languages import javascript, python, typescript
from app.contracts.structure import AnalysisBudget, FileObservationsV1, SourceUnitV1


def extract_syntax(unit: SourceUnitV1, budget: AnalysisBudget) -> FileObservationsV1:
    if unit.language == "python":
        return python.extract(unit, budget)
    if unit.language in ("javascript", "jsx"):
        return javascript.extract(unit, budget)
    if unit.language in ("typescript", "tsx"):
        return typescript.extract(unit, budget)
    raise ValueError(f"unsupported language: {unit.language}")
