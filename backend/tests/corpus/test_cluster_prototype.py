"""The clustering prototype is deterministic and never reports worse modularity than singletons."""

from app.analyzers.graph import build_graph
from evals.ast import cluster_eval

from tests.phase3.unit.test_dependency_graph import entries

FILES = {
    "a/one.py": "from a import two\n",
    "a/two.py": "from a import one\n",
    "b/three.py": "from b import four\n",
    "b/four.py": "from b import three\nfrom a import one\n",
}


def test_greedy_modularity_is_deterministic_and_finds_the_two_groups():
    graph = build_graph(entries(FILES), list(FILES))
    edges = cluster_eval.undirected_edges(graph)
    files = sorted(n.id for n in graph.nodes if n.kind == "file")
    first = cluster_eval.greedy_modularity(files, edges)
    assert first == cluster_eval.greedy_modularity(list(reversed(files)), edges)
    assert first["file:a/one.py"] == first["file:a/two.py"]
    assert first["file:b/three.py"] == first["file:b/four.py"]
    singletons = {f: f for f in files}
    assert cluster_eval.modularity(edges, first) >= cluster_eval.modularity(edges, singletons)


def test_a_graph_without_edges_stays_as_singletons():
    graph = build_graph(entries({"x.py": "y = 1\n", "z.py": "w = 2\n"}), ["x.py", "z.py"])
    assert cluster_eval.compare(graph)["greedy_components"] == 2
