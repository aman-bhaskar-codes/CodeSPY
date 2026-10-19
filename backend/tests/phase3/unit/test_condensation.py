import pytest
from app.core.dsa.condensation import condense
from app.core.dsa.projection import build_projection
from app.core.dsa.puretypes import GraphBudget, GraphEdge
from hypothesis import given
from hypothesis import strategies as st


def test_stable_dag_order_and_metadata():
    graph = build_projection(
        ["a", "b", "c", "z"],
        [GraphEdge("1", "a", "b"), GraphEdge("2", "b", "a"), GraphEdge("3", "b", "c")],
        relation_family="imports",
        source_coverage="partial",
    )
    result = condense(graph)
    assert result.components == (("a", "b"), ("c",), ("z",))
    assert result.edges == ((0, 1),)
    assert result.topological_order == (0, 1, 2)
    assert result.source_coverage == "partial"


@pytest.mark.parametrize("scans", [0, 2, 3, 4])
def test_condensation_budget_discards_entire_result(scans):
    graph = build_projection(["a", "b"], [GraphEdge("1", "a", "b")], relation_family="calls")
    result = condense(
        graph,
        budget=GraphBudget(max_scanned_edges=scans),
    )
    assert result.disposition == "unavailable_budget"
    assert result.components == result.edges == result.topological_order == ()


def test_cancellation_including_kahn_is_unavailable():
    graph = build_projection(["a", "b", "z"], [GraphEdge("1", "a", "b")], relation_family="calls")
    for checkpoint in range(1, 35):
        ticks = 0

        def cancelled(limit=checkpoint):
            nonlocal ticks
            ticks += 1
            return ticks == limit

        result = condense(graph, cancelled=cancelled)
        if ticks >= checkpoint:
            assert result.reason == "cancelled"
            assert result.components == result.edges == result.topological_order == ()


@given(st.sets(st.tuples(st.integers(0, 7), st.integers(0, 7)), max_size=40))
def test_generated_condensation_is_dag(edges):
    graph = build_projection(
        map(str, range(8)),
        [GraphEdge(str(i), str(a), str(b)) for i, (a, b) in enumerate(sorted(edges))],
        relation_family="imports",
    )
    result = condense(graph)
    assert result.disposition == "complete"
    positions = {node: i for i, node in enumerate(result.topological_order)}
    assert len(positions) == len(result.components)
    assert all(positions[a] < positions[b] for a, b in result.edges)
    owners = {node: i for i, component in enumerate(result.components) for node in component}
    assert set(result.edges) == {
        (owners[str(a)], owners[str(b)]) for a, b in edges if owners[str(a)] != owners[str(b)]
    }
