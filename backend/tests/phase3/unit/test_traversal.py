import networkx as nx
import pytest
from app.core.dsa.projection import build_projection
from app.core.dsa.puretypes import GraphBudget, GraphEdge
from app.core.dsa.traversal import traverse
from hypothesis import given
from hypothesis import strategies as st


def test_forward_reverse_parallel_isolates():
    graph = build_projection(
        ["a", "b", "z"],
        [GraphEdge("1", "a", "b"), GraphEdge("2", "a", "b")],
        relation_family="calls",
    )
    assert traverse(graph, ["a"]).nodes == ("a", "b")
    assert traverse(graph, ["b"], direction="reverse").nodes == ("b", "a")
    assert len(graph.edges) == 2
    assert traverse(graph, ["z"]).nodes == ("z",)


def test_node_budget_is_explicit():
    graph = build_projection(["a", "b"], [GraphEdge("1", "a", "b")], relation_family="calls")
    result = traverse(graph, ["a"], budget=GraphBudget(max_nodes=1))
    assert result.disposition == "truncated"
    assert result.reason == "nodes"


@pytest.mark.parametrize(
    ("limit", "reason"),
    [
        ("max_edges", "edges"),
        ("max_scanned_edges", "scanned_edges"),
        ("max_frontier", "frontier"),
        ("max_depth", "depth"),
        ("wall_seconds", "wall"),
    ],
)
def test_each_budget(limit, reason):
    graph = build_projection(["a", "b"], [GraphEdge("1", "a", "b")], relation_family="calls")
    result = traverse(graph, ["a"], budget=GraphBudget(**{limit: 0}))
    assert (result.disposition, result.reason) == ("truncated", reason)


def test_cancelled_and_dfs_deterministic():
    graph = build_projection(
        ["a", "b", "c"],
        [GraphEdge("1", "a", "b"), GraphEdge("2", "a", "c")],
        relation_family="calls",
    )
    assert traverse(graph, ["a"], strategy="dfs").nodes == ("a", "c", "b")
    assert traverse(graph, ["a"], cancelled=lambda: True).reason == "cancelled"


def test_projection_is_immutable_and_preserves_support():
    graph = build_projection(
        ["b", "a", "a"],
        [
            GraphEdge("1", "a", "b", support=("x",)),
            GraphEdge("1", "a", "b", support=("y",)),
            GraphEdge("1", "a", "b", support=("x",)),
        ],
        relation_family="calls",
    )
    assert graph.nodes == ("a", "b")
    assert len(graph.edges) == 2
    with pytest.raises(TypeError):
        graph.forward["a"] = ()
    with pytest.raises(ValueError, match="endpoint"):
        build_projection(["a"], [GraphEdge("1", "a", "b")], relation_family="calls")


def test_empty_graph_and_seed():
    graph = build_projection([], [], relation_family="calls")
    assert traverse(graph, []).disposition == "complete"
    with pytest.raises(ValueError, match="seed"):
        traverse(graph, ["a"])


def test_partial_traversal_edges_have_returned_endpoints():
    graph = build_projection(
        ["a", "b", "c"],
        [GraphEdge("1", "a", "b"), GraphEdge("2", "a", "c")],
        relation_family="calls",
    )
    result = traverse(graph, ["a"], budget=GraphBudget(max_edges=1))
    assert result.disposition == "truncated"
    assert all(
        edge.source in result.nodes and edge.target in result.nodes
        for edge in result.traversal_edges
    )


@given(st.sets(st.tuples(st.integers(0, 7), st.integers(0, 7)), max_size=40))
def test_reachability_matches_reference(edges):
    graph = build_projection(
        map(str, range(8)),
        [GraphEdge(str(i), str(a), str(b)) for i, (a, b) in enumerate(sorted(edges))],
        relation_family="calls",
    )
    reference = nx.DiGraph()
    reference.add_nodes_from(graph.nodes)
    reference.add_edges_from((str(a), str(b)) for a, b in edges)
    for strategy in ("bfs", "dfs"):
        for direction in ("forward", "reverse"):
            result = traverse(graph, ["0"], strategy=strategy, direction=direction)
            expected = nx.descendants(
                reference if direction == "forward" else reference.reverse(), "0"
            ) | {"0"}
            assert set(result.nodes) == expected
            assert result.disposition == "complete"
