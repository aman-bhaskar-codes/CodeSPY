"""Iterative Kosaraju, publishing a partition only after both passes finish."""

from collections.abc import Callable

from .puretypes import (
    DEFAULT_CLOCK,
    BudgetStopError,
    GraphBudget,
    GraphProjection,
    GraphResult,
    Work,
    never_cancelled,
)


def _components(graph: GraphProjection, work: Work) -> tuple[tuple[str, ...], ...]:
    work.check()
    if len(graph.nodes) > work.budget.max_nodes:
        raise BudgetStopError("nodes")
    if len(graph.edges) > work.budget.max_edges:
        raise BudgetStopError("edges")
    seen: set[str] = set()
    finished: list[str] = []
    for start in graph.nodes:
        work.check()
        if start in seen:
            continue
        seen.add(start)
        stack = [(start, 0)]
        while stack:
            work.check(frontier=len(stack))
            node, index = stack[-1]
            if index == len(graph.forward[node]):
                finished.append(node)
                stack.pop()
                continue
            edge = graph.forward[node][index]
            stack[-1] = (node, index + 1)
            work.scan()
            if edge.target not in seen:
                work.check(frontier=len(stack) + 1)
                seen.add(edge.target)
                stack.append((edge.target, 0))
    seen.clear()
    components = []
    for start in reversed(finished):
        work.check()
        if start in seen:
            continue
        pending = [start]
        seen.add(start)
        members = []
        while pending:
            work.check(frontier=len(pending))
            node = pending.pop()
            members.append(node)
            for edge in graph.reverse[node]:
                work.scan()
                if edge.source not in seen:
                    work.check(frontier=len(pending) + 1)
                    seen.add(edge.source)
                    pending.append(edge.source)
        components.append(tuple(sorted(members)))
    return tuple(sorted(components))


def strong_components(
    graph: GraphProjection,
    *,
    budget: GraphBudget | None = None,
    cancelled: Callable[[], bool] = never_cancelled,
    clock: Callable[[], float] = DEFAULT_CLOCK,
) -> GraphResult:
    work = Work(budget or GraphBudget(), cancelled, clock)
    try:
        components = _components(graph, work)
        work.check()
        return work.result(graph, components=components)
    except BudgetStopError as exc:
        return work.result(graph, str(exc))
