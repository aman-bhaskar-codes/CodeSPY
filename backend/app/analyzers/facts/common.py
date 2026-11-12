"""Helpers shared by the per-language fact extractors."""

from app.contracts.ast_facts import DiagnosticFactV1
from app.contracts.structure import AnalysisBudget
from tree_sitter import Node


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def text(data: bytes, node: Node | None) -> str:
    return "" if node is None else data[node.start_byte : node.end_byte].decode("utf-8")


def count_errors(root: Node, budget: AnalysisBudget) -> int:
    """Whole-tree pass: charge the node budget and count syntax error nodes."""
    errors = 0
    stack = [root]
    while stack:
        node = stack.pop()
        budget.tick_nodes(1)
        if node.is_error or node.is_missing:
            errors += 1
        stack.extend(node.children)
    return errors


def file_diagnostics(errors: int, truncated: bool) -> tuple[DiagnosticFactV1, ...]:
    found: list[DiagnosticFactV1] = []
    if truncated:
        found.append(
            DiagnosticFactV1(code="fact_capacity", message="fact capacity reached; file truncated")
        )
    if errors:
        found.append(DiagnosticFactV1(code="syntax_error", message=f"{errors} syntax error nodes"))
    return tuple(found)
