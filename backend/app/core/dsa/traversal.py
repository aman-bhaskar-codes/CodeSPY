"""Bounded iterative reachability; partial answers are explicitly truncated."""

from collections import deque
from collections.abc import Callable, Iterable
from typing import Literal

from .puretypes import (
    DEFAULT_CLOCK,
    BudgetStopError,
    GraphBudget,
    GraphEdge,
    GraphProjection,
    GraphResult,
    Work,
    never_cancelled,
)


def traverse(
    graph: GraphProjection,
    seeds: Iterable[str],
    *,
    strategy: Literal["bfs", "dfs"] = "bfs",
    direction: Literal["forward", "reverse"] = "forward",
    budget: GraphBudget | None = None,
    cancelled: Callable[[], bool] = never_cancelled,
    clock: Callable[[], float] = DEFAULT_CLOCK,
) -> GraphResult:
    if strategy not in ("bfs", "dfs") or direction not in ("forward", "reverse"):
        raise ValueError("invalid traversal strategy/direction")
    budget = budget or GraphBudget(max_nodes=500, max_edges=1000, max_scanned_edges=10000)
    adjacency = graph.forward if direction == "forward" else graph.reverse
    work = Work(budget, cancelled, clock)
    pending: deque[tuple[str, int]] = deque()
    seen: set[str] = set()
    output: list[str] = []
    edges: dict[GraphEdge, None] = {}
    reason = None
    try:
        for seed in sorted(set(seeds)):
            work.check()
            if seed not in adjacency:
                raise ValueError("seed outside projection")
            if len(seen) >= budget.max_nodes:
                raise BudgetStopError("nodes")
            work.check(frontier=len(pending) + 1)
            seen.add(seed)
            pending.append((seed, 0))
        while pending:
            work.check(frontier=len(pending))
            node, depth = pending.popleft() if strategy == "bfs" else pending.pop()
            output.append(node)
            for edge in adjacency[node]:
                work.scan()
                target = edge.target if direction == "forward" else edge.source
                if target not in seen:
                    work.check(depth=depth + 1, frontier=len(pending) + 1)
                    if len(seen) >= budget.max_nodes:
                        raise BudgetStopError("nodes")
                if edge not in edges and len(edges) >= budget.max_edges:
                    raise BudgetStopError("edges")
                edges[edge] = None
                if target not in seen:
                    seen.add(target)
                    pending.append((target, depth + 1))
    except BudgetStopError as exc:
        reason = str(exc)
        # Queued nodes were admitted before their provenance edges; retain their endpoints.
        remaining = pending if strategy == "bfs" else reversed(pending)
        output.extend(node for node, _ in remaining)
    return work.result(
        graph, reason, traversal=True, nodes=tuple(output), traversal_edges=tuple(edges)
    )
