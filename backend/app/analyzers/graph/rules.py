"""Pure evaluation of architecture rules over a report's dependency graph."""

from collections import defaultdict, deque

from app.contracts.architecture_report import DependencyGraphV1, GraphEdgeV1
from app.contracts.architecture_rules import (
    MAX_VIOLATIONS,
    RuleCheckV1,
    RuleResultV1,
    RuleSetV1,
    RuleStatus,
    RuleV1,
    ViolationV1,
)

CHAIN_LIMIT = 12


def _path(node_id: str) -> str:
    """File and package nodes carry their path after the id prefix; external ones have none."""
    if node_id.startswith("file:"):
        return node_id[len("file:") :]
    if node_id.startswith("pkg:"):
        return node_id[len("pkg:") :]
    return ""


def _in(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path == p or path.startswith(p + "/") for p in prefixes)


def _group_of(path: str, groups: tuple[tuple[str, ...], ...]) -> int | None:
    for index, prefixes in enumerate(groups):
        if _in(path, prefixes):
            return index
    return None


class _Index:
    def __init__(self, graph: DependencyGraphV1) -> None:
        self.out: dict[str, list[GraphEdgeV1]] = defaultdict(list)
        self.unknown_from: dict[str, int] = defaultdict(int)
        self.files = [n.id for n in graph.nodes if n.kind == "file"]
        for edge in graph.edges:
            if edge.resolution == "resolved" and edge.target is not None:
                self.out[edge.source].append(edge)
            elif edge.resolution in {"ambiguous", "unresolved"}:
                self.unknown_from[edge.source] += 1

    def reach(self, start: str) -> dict[str, tuple[str, GraphEdgeV1]]:
        """Breadth-first parents over resolved edges: node -> (previous node, edge used)."""
        parents: dict[str, tuple[str, GraphEdgeV1]] = {}
        seen = {start}
        queue = deque([start])
        while queue:
            node = queue.popleft()
            for edge in self.out.get(node, ()):
                assert edge.target is not None
                if edge.target in seen:
                    continue
                seen.add(edge.target)
                parents[edge.target] = (node, edge)
                queue.append(edge.target)
        return parents


def _chain(start: str, end: str, parents: dict[str, tuple[str, GraphEdgeV1]]) -> list[str]:
    nodes = [end]
    while nodes[-1] != start:
        nodes.append(parents[nodes[-1]][0])
    return list(reversed(nodes))


def _violation(chain: list[str], parents: dict[str, tuple[str, GraphEdgeV1]]) -> ViolationV1:
    first = parents[chain[1]][1]
    shown = tuple(_path(n) for n in chain[:CHAIN_LIMIT])
    return ViolationV1(
        source=_path(chain[0]), target=_path(chain[-1]), chain=shown, evidence=first.evidence
    )


def _evaluate(rule: RuleV1, index: _Index) -> RuleResultV1:
    groups = rule.groups
    members: list[list[str]] = [[] for _ in groups]
    for node in index.files:
        g = _group_of(_path(node), groups)
        if g is not None:
            members[g].append(node)
    violations: list[ViolationV1] = []
    total = 0

    def add(chain: list[str], parents: dict[str, tuple[str, GraphEdgeV1]]) -> None:
        nonlocal total
        total += 1
        if len(violations) < MAX_VIOLATIONS:
            violations.append(_violation(chain, parents))

    if rule.kind == "layers":
        # A lower layer may not import a higher one: check direct imports out of each lower layer.
        for g in range(1, len(groups)):
            for node in members[g]:
                for edge in index.out.get(node, ()):
                    assert edge.target is not None
                    target_group = _group_of(_path(edge.target), groups)
                    if target_group is not None and target_group < g:
                        add([node, edge.target], {edge.target: (node, edge)})
    else:
        pairs = (
            [(0, 1)]
            if rule.kind == "forbidden"
            else [(a, b) for a in range(len(groups)) for b in range(len(groups)) if a != b]
        )
        for a, b in pairs:
            for node in members[a]:
                if rule.allow_indirect:
                    for edge in index.out.get(node, ()):
                        assert edge.target is not None
                        if _group_of(_path(edge.target), groups) == b:
                            add([node, edge.target], {edge.target: (node, edge)})
                    continue
                parents = index.reach(node)
                for target in sorted(parents):
                    if _group_of(_path(target), groups) == b:
                        add(_chain(node, target, parents), parents)
    scope = {n for group in members for n in group}
    unknown = sum(index.unknown_from.get(n, 0) for n in scope)
    status: RuleStatus = "violated" if total else ("unknown" if unknown else "passed")
    return RuleResultV1(
        name=rule.name,
        kind=rule.kind,
        status=status,
        violations_total=total,
        violations=tuple(violations),
        unknown_imports=unknown,
        files_in_scope=len(scope),
    )


def check_rules(
    graph: DependencyGraphV1, rules: RuleSetV1, report_id: str, inputs_digest: str
) -> RuleCheckV1:
    index = _Index(graph)
    return RuleCheckV1(
        report_id=report_id,
        inputs_digest=inputs_digest,
        results=tuple(_evaluate(rule, index) for rule in rules.rules),
    )
