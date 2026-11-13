"""Prototype: does deterministic greedy modularity beat directory components on real repositories?

Greedy agglomeration (Clauset-Newman-Moore) over the undirected resolved file graph: start with
one cluster per file and merge the pair with the largest modularity gain until no merge gains.
Ties break by smallest cluster ids, so the result is a pure function of the graph. Directory
components are the report's current grouping. Metrics: modularity Q of each grouping, share of
resolved edges that cross component boundaries, and component counts.
"""

from collections import defaultdict
from typing import Any

from app.analyzers.graph.analytics import analyze
from app.contracts.architecture_report import DependencyGraphV1


def undirected_edges(graph: DependencyGraphV1) -> dict[tuple[str, str], int]:
    files = {n.id for n in graph.nodes if n.kind == "file"}
    weights: dict[tuple[str, str], int] = defaultdict(int)
    for e in graph.edges:
        if e.resolution == "resolved" and e.target in files and e.source != e.target:
            a, b = sorted((e.source, e.target))
            weights[(a, b)] += 1
    return dict(weights)


def modularity(edges: dict[tuple[str, str], int], group: dict[str, str]) -> float:
    m = sum(edges.values())
    if m == 0:
        return 0.0
    inside: dict[str, float] = defaultdict(float)
    degree: dict[str, float] = defaultdict(float)
    for (a, b), w in edges.items():
        degree[group[a]] += w
        degree[group[b]] += w
        if group[a] == group[b]:
            inside[group[a]] += w
    return sum(inside[c] / m - (degree[c] / (2 * m)) ** 2 for c in degree)


def greedy_modularity(nodes: list[str], edges: dict[tuple[str, str], int]) -> dict[str, str]:
    m = sum(edges.values())
    if m == 0:
        return {n: n for n in nodes}
    cluster = {n: n for n in sorted(nodes)}
    weight: dict[tuple[str, str], float] = {k: float(v) for k, v in edges.items()}
    degree: dict[str, float] = defaultdict(float)
    for (a, b), w in edges.items():
        degree[a] += w
        degree[b] += w
    while True:
        best: tuple[float, str, str] | None = None
        for (a, b), w in sorted(weight.items()):
            gain = w / m - degree[a] * degree[b] / (2 * m * m)
            if gain > 1e-12 and (best is None or gain > best[0] + 1e-15):
                best = (gain, a, b)
        if best is None:
            break
        _, keep, drop = best
        for node, c in cluster.items():
            if c == drop:
                cluster[node] = keep
        merged: dict[tuple[str, str], float] = defaultdict(float)
        for (a, b), w in weight.items():
            a2, b2 = (keep if a == drop else a), (keep if b == drop else b)
            if a2 == b2:
                continue
            merged[tuple(sorted((a2, b2)))] += w  # type: ignore[index]
        weight = dict(merged)
        degree[keep] += degree.pop(drop)
    return cluster


def compare(graph: DependencyGraphV1) -> dict[str, Any]:
    edges = undirected_edges(graph)
    files = sorted(n.id for n in graph.nodes if n.kind == "file")
    directory = analyze(graph).component_by_file
    directory = {f: directory.get(f, "?") for f in files}
    greedy = greedy_modularity(files, edges)
    total = sum(edges.values())

    def crossing(group: dict[str, str]) -> float:
        return (
            sum(w for (a, b), w in edges.items() if group[a] != group[b]) / total if total else 0.0
        )

    return {
        "files": len(files),
        "edges": total,
        "dir_components": len(set(directory.values())),
        "dir_q": round(modularity(edges, directory), 3),
        "dir_cross": round(crossing(directory), 3),
        "greedy_components": len(set(greedy.values())),
        "greedy_q": round(modularity(edges, greedy), 3),
        "greedy_cross": round(crossing(greedy), 3),
    }
