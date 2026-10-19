import networkx as nx
import pytest
from app.core.dsa.projection import build_projection
from app.core.dsa.puretypes import GraphBudget, GraphEdge
from app.core.dsa.scc import strong_components
from hypothesis import given
from hypothesis import strategies as st


@given(st.sets(st.tuples(st.integers(0, 7), st.integers(0, 7)), max_size=40))
def test_partition_matches_reference(edges):
    graph = build_projection(
        map(str, range(8)),
        [GraphEdge(str(i), str(a), str(b)) for i, (a, b) in enumerate(sorted(edges))],
        relation_family="calls",
    )
    result = strong_components(graph)
    reference = nx.DiGraph()
    reference.add_nodes_from(graph.nodes)
    reference.add_edges_from((str(a), str(b)) for a, b in edges)
    assert {frozenset(c) for c in result.components} == {
        frozenset(c) for c in nx.strongly_connected_components(reference)
    }
    assert sum(map(len, result.components)) == len(graph.nodes)

    def reachable(start):
        seen = {start}
        pending = [start]
        while pending:
            current = pending.pop()
            for source, target in edges:
                if str(source) == current and str(target) not in seen:
                    seen.add(str(target))
                    pending.append(str(target))
        return seen

    owners = {node: i for i, component in enumerate(result.components) for node in component}
    for a in graph.nodes:
        for b in graph.nodes:
            assert (owners[a] == owners[b]) == (b in reachable(a) and a in reachable(b))


def test_incomplete_has_no_components():
    graph = build_projection(["a", "b"], [GraphEdge("1", "a", "b")], relation_family="imports")
    result = strong_components(graph, budget=GraphBudget(max_scanned_edges=1))
    assert result.disposition == "unavailable_budget"
    assert result.components == ()


@pytest.mark.parametrize("scans", [0, 1, 2, 3])
def test_scan_exhaustion_both_passes(scans):
    graph = build_projection(
        ["a", "b"], [GraphEdge("1", "a", "b"), GraphEdge("2", "b", "a")], relation_family="calls"
    )
    result = strong_components(graph, budget=GraphBudget(max_scanned_edges=scans))
    assert result.components == ()
    assert result.reason == "scanned_edges"


@pytest.mark.parametrize("limit", ["max_nodes", "max_edges", "max_frontier", "wall_seconds"])
def test_other_scc_limits(limit):
    graph = build_projection(["a", "b"], [GraphEdge("1", "a", "b")], relation_family="calls")
    assert strong_components(graph, budget=GraphBudget(**{limit: 0})).components == ()


@pytest.mark.parametrize("stop", [1, 9, 15])
def test_cancellation_never_publishes_prefix(stop):
    graph = build_projection(
        ["a", "b"], [GraphEdge("1", "a", "b"), GraphEdge("2", "b", "a")], relation_family="calls"
    )
    calls = 0

    def cancelled():
        nonlocal calls
        calls += 1
        return calls >= stop

    result = strong_components(graph, cancelled=cancelled)
    assert result.components == ()
    assert result.reason == "cancelled"


def test_deep_graph_is_iterative():
    nodes = list(map(str, range(900)))
    graph = build_projection(
        nodes,
        [GraphEdge(str(i), nodes[i], nodes[i + 1]) for i in range(899)],
        relation_family="calls",
    )
    result = strong_components(graph)
    assert len(result.components) == 900


def test_cancel_and_deadline_at_every_cooperative_checkpoint():
    graph = build_projection(
        ["a", "b", "c"],
        [GraphEdge("1", "a", "b"), GraphEdge("2", "b", "a")],
        relation_family="calls",
    )
    for checkpoint in range(1, 35):
        ticks = 0

        def cancelled(limit=checkpoint):
            nonlocal ticks
            ticks += 1
            return ticks == limit

        result = strong_components(graph, cancelled=cancelled)
        if ticks >= checkpoint:
            assert result.components == ()
            assert result.reason == "cancelled"
        ticks = 0

        def clock(limit=checkpoint):
            nonlocal ticks
            ticks += 1
            return 0.0 if ticks <= limit else 3.0

        result = strong_components(graph, clock=clock)
        if ticks > checkpoint:
            assert result.components == ()
            assert result.reason == "wall"
