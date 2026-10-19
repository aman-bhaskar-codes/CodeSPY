"""Deterministic adjacency, preserving parallel provenance edges and isolates."""

from collections.abc import Iterable
from types import MappingProxyType

from .puretypes import GraphEdge, GraphProjection


def build_projection(
    nodes: Iterable[str],
    edges: Iterable[GraphEdge],
    *,
    relation_family: str,
    source_coverage: str = "complete",
    scope_id: str = "",
    omitted_edge_reasons: tuple[str, ...] = (),
) -> GraphProjection:
    selected = tuple(
        sorted(set(edges), key=lambda e: (e.source, e.target, e.relation, e.id, e.support))
    )
    identities = tuple(sorted(set(nodes)))
    forward: dict[str, list[GraphEdge]] = {node: [] for node in identities}
    reverse: dict[str, list[GraphEdge]] = {node: [] for node in identities}
    for edge in selected:
        if edge.source not in forward or edge.target not in reverse:
            raise ValueError("edge endpoint outside projection")
        forward[edge.source].append(edge)
        reverse[edge.target].append(edge)
    return GraphProjection(
        identities,
        selected,
        MappingProxyType({n: tuple(e) for n, e in forward.items()}),
        MappingProxyType({n: tuple(e) for n, e in reverse.items()}),
        relation_family,
        source_coverage,
        scope_id,
        tuple(sorted(set(omitted_edge_reasons))),
    )
