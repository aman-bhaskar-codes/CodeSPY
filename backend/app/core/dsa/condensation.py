"""Unique inter-component edges and deterministic Kahn ordering."""

import heapq
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
from .scc import _components


def condense(
    graph: GraphProjection,
    *,
    budget: GraphBudget | None = None,
    cancelled: Callable[[], bool] = never_cancelled,
    clock: Callable[[], float] = DEFAULT_CLOCK,
) -> GraphResult:
    work = Work(budget or GraphBudget(), cancelled, clock)
    try:
        components = _components(graph, work)
        owners = {node: i for i, members in enumerate(components) for node in members}
        edges: set[tuple[int, int]] = set()
        for edge in graph.edges:
            work.scan()
            source, target = owners[edge.source], owners[edge.target]
            if source != target:
                edges.add((source, target))
        outgoing: list[list[int]] = [[] for _ in components]
        degree = [0] * len(components)
        for source, target in sorted(edges):
            work.scan()
            outgoing[source].append(target)
            degree[target] += 1
        pending = [i for i, count in enumerate(degree) if count == 0]
        heapq.heapify(pending)
        order = []
        while pending:
            work.check(frontier=len(pending))
            source = heapq.heappop(pending)
            order.append(source)
            for target in outgoing[source]:
                work.scan()
                degree[target] -= 1
                if degree[target] == 0:
                    work.check(frontier=len(pending) + 1)
                    heapq.heappush(pending, target)
        work.check()
        return work.result(
            graph, components=components, edges=tuple(sorted(edges)), topological_order=tuple(order)
        )
    except BudgetStopError as exc:
        return work.result(graph, str(exc))
