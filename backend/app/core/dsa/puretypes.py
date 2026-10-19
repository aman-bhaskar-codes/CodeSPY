"""Infrastructure-free bounded graph values and cooperative work accounting."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from time import monotonic


@dataclass(frozen=True, order=True)
class GraphEdge:
    id: str
    source: str
    target: str
    relation: str = "CALLS"
    support: tuple[str, ...] = ()


@dataclass(frozen=True)
class GraphBudget:
    max_nodes: int = 1000
    max_edges: int = 2000
    max_scanned_edges: int = 20000
    max_frontier: int = 1000
    max_depth: int = 16
    wall_seconds: float = 2.0

    def __post_init__(self) -> None:
        if (
            min(
                self.max_nodes,
                self.max_edges,
                self.max_scanned_edges,
                self.max_frontier,
                self.max_depth,
                self.wall_seconds,
            )
            < 0
        ):
            raise ValueError("graph budgets must be nonnegative")


@dataclass(frozen=True)
class GraphProjection:
    nodes: tuple[str, ...]
    edges: tuple[GraphEdge, ...]
    forward: Mapping[str, tuple[GraphEdge, ...]]
    reverse: Mapping[str, tuple[GraphEdge, ...]]
    relation_family: str
    source_coverage: str
    scope_id: str
    omitted_edge_reasons: tuple[str, ...]


@dataclass(frozen=True)
class GraphResult:
    disposition: str
    reason: str | None
    relation_family: str
    source_coverage: str
    scope_id: str
    omitted_edge_reasons: tuple[str, ...]
    scanned_edges: int
    nodes: tuple[str, ...] = ()
    traversal_edges: tuple[GraphEdge, ...] = ()
    components: tuple[tuple[str, ...], ...] = ()
    edges: tuple[tuple[int, int], ...] = ()
    topological_order: tuple[int, ...] = ()


class BudgetStopError(Exception):
    pass


class Work:
    def __init__(
        self, budget: GraphBudget, cancelled: Callable[[], bool], clock: Callable[[], float]
    ) -> None:
        self.budget = budget
        self.cancelled = cancelled
        self.clock = clock
        self.deadline = clock() + budget.wall_seconds
        self.scanned = 0

    def check(self, *, frontier: int = 0, depth: int = 0) -> None:
        if self.cancelled():
            raise BudgetStopError("cancelled")
        if self.clock() >= self.deadline:
            raise BudgetStopError("wall")
        if frontier > self.budget.max_frontier:
            raise BudgetStopError("frontier")
        if depth > self.budget.max_depth:
            raise BudgetStopError("depth")

    def scan(self) -> None:
        self.check()
        if self.scanned >= self.budget.max_scanned_edges:
            raise BudgetStopError("scanned_edges")
        self.scanned += 1

    def result(
        self,
        graph: GraphProjection,
        reason: str | None = None,
        *,
        traversal: bool = False,
        nodes: tuple[str, ...] = (),
        traversal_edges: tuple[GraphEdge, ...] = (),
        components: tuple[tuple[str, ...], ...] = (),
        edges: tuple[tuple[int, int], ...] = (),
        topological_order: tuple[int, ...] = (),
    ) -> GraphResult:
        disposition = "complete"
        if reason:
            disposition = (
                "truncated"
                if traversal
                else "unavailable_cancelled"
                if reason == "cancelled"
                else "unavailable_budget"
            )
        return GraphResult(
            disposition=disposition,
            reason=reason,
            relation_family=graph.relation_family,
            source_coverage=graph.source_coverage,
            scope_id=graph.scope_id,
            omitted_edge_reasons=graph.omitted_edge_reasons,
            scanned_edges=self.scanned,
            nodes=nodes,
            traversal_edges=traversal_edges,
            components=components,
            edges=edges,
            topological_order=topological_order,
        )


def never_cancelled() -> bool:
    return False


DEFAULT_CLOCK = monotonic
