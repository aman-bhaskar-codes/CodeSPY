"""Deterministic structure derived from the dependency graph: cycles, hubs, components, layers.

Everything here is a derivation from resolved edges only. Ambiguous, external and unresolved edges
never create a dependency between two repository files; they are reported separately as unknowns
or external dependencies, so a missing edge is never read as "no dependency".
"""

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from app.analyzers.graph.index import FILE, PACKAGE
from app.contracts.architecture_report import MAX_EDGES, MAX_NODES, DependencyGraphV1
from app.core.dsa.condensation import condense
from app.core.dsa.projection import build_projection
from app.core.dsa.puretypes import GraphBudget, GraphEdge
from app.core.dsa.scc import strong_components

MAX_COMPONENTS = 40
MIN_COMPONENTS = 6
SPLIT_FILES = 10
TOP = 10
ENTRY_NAMES = frozenset(
    {"main", "index", "app", "server", "cli", "__main__", "manage", "run", "program", "start"}
)


@dataclass(frozen=True)
class Hub:
    node: str
    count: int


@dataclass(frozen=True)
class ComponentSummary:
    name: str
    files: int
    languages: tuple[tuple[str, int], ...]
    inbound: int  # resolved edges from other components
    outbound: int
    depends_on: tuple[tuple[str, int], ...]  # top neighbors by edge count
    layer: int | None


@dataclass(frozen=True)
class Analytics:
    disposition: str  # complete, or the reason a bounded computation stopped early
    file_cycles: tuple[tuple[str, ...], ...] = ()
    component_cycles: tuple[tuple[str, ...], ...] = ()
    hubs_in: tuple[Hub, ...] = ()
    hubs_out: tuple[Hub, ...] = ()
    components: tuple[ComponentSummary, ...] = ()
    layers: tuple[tuple[str, ...], ...] = ()
    entry_candidates: tuple[str, ...] = ()
    external: tuple[tuple[str, int], ...] = ()  # (external node id, importing files)
    counts: dict[str, int] = field(default_factory=dict)
    component_by_file: dict[str, str] = field(default_factory=dict)  # file node id -> component


def _budget() -> GraphBudget:
    return GraphBudget(
        max_nodes=MAX_NODES,
        max_edges=MAX_EDGES,
        max_scanned_edges=MAX_EDGES * 6,
        max_frontier=MAX_NODES,
        max_depth=MAX_NODES,
        wall_seconds=120.0,  # safety net only: the count bounds decide every real outcome
    )


def _directory(node_id: str) -> str:
    """The directory a file or package node lives in ('' is the repository root)."""
    if node_id.startswith(PACKAGE):
        value = node_id[len(PACKAGE) :]
        return "" if value == "." else value
    path = node_id[len(FILE) :]
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _kept_segments(directories: set[str]) -> dict[str, tuple[str, ...]]:
    """Directory -> its informative segments (single-child, file-less directories are dropped)."""
    children: dict[str, set[str]] = defaultdict(set)
    has_files: set[str] = set()
    for directory in directories:
        parts = directory.split("/") if directory else []
        for i in range(len(parts)):
            children["/".join(parts[:i])].add(parts[i])
        has_files.add(directory)
    kept: dict[str, tuple[str, ...]] = {}
    for directory in directories:
        parts = directory.split("/") if directory else []
        result = []
        for i, part in enumerate(parts):
            parent = "/".join(parts[:i])
            unary = len(children[parent]) == 1 and parent not in has_files
            if not unary or i == len(parts) - 1:
                result.append(part)
        kept[directory] = tuple(result)
    return kept


def assign_components(file_dirs: Counter[str]) -> dict[str, str]:
    """Group directories into at most ``MAX_COMPONENTS`` architectural components.

    Start from the first informative path segment and keep splitting the largest component by its
    next segment while the result is still coarse; ties break by path so output is stable.
    """
    kept = _kept_segments(set(file_dirs))
    depth: dict[str, int] = {}  # component key -> number of kept segments used

    def key(directory: str, n: int) -> str:
        return "/".join(kept[directory][:n]) or "."

    members: dict[str, list[str]] = defaultdict(list)
    for directory in sorted(file_dirs):
        members[key(directory, 1)].append(directory)
        depth[key(directory, 1)] = 1

    def files_of(component: str) -> int:
        return sum(file_dirs[d] for d in members[component])

    while True:
        total = len(members)
        if total >= MAX_COMPONENTS:
            break
        candidates = [
            c
            for c in sorted(members)
            if files_of(c) > SPLIT_FILES
            and any(len(kept[d]) > depth[c] for d in members[c])
            and (total < MIN_COMPONENTS or files_of(c) > 3 * SPLIT_FILES)
        ]
        if not candidates:
            break
        target = max(candidates, key=lambda c: (files_of(c), c))
        n = depth[target] + 1
        groups: dict[str, list[str]] = defaultdict(list)
        for directory in members[target]:
            groups[key(directory, n)].append(directory)
        if len(groups) == 1 or total - 1 + len(groups) > MAX_COMPONENTS:
            depth[target] = n  # nothing to split at this depth; look one level deeper
            if all(len(kept[d]) <= n for d in members[target]):
                depth[target] = 10**6  # exhausted
            continue
        del members[target]
        depth.pop(target)
        for name, dirs in groups.items():
            members[name] = dirs
            depth[name] = n
    return {d: c for c, dirs in members.items() for d in dirs}


def analyze(graph: DependencyGraphV1, budget: GraphBudget | None = None) -> Analytics:
    limits = budget or _budget()
    files = [n for n in graph.nodes if n.kind == "file"]
    file_ids = {n.id for n in files}
    resolved = [e for e in graph.edges if e.resolution == "resolved" and e.target is not None]
    # File-level dependency edges (a file importing a package depends on the package, handled at
    # component level because package nodes have no outgoing edges).
    file_edges = {
        (e.source, e.target) for e in resolved if e.target in file_ids and e.source != e.target
    }
    indeg: Counter[str] = Counter()
    outdeg: Counter[str] = Counter()
    for source, target in file_edges:
        outdeg[source] += 1
        indeg[target] += 1
    for e in resolved:
        if e.target is not None and e.target.startswith(PACKAGE):
            outdeg[e.source] += 1

    file_dirs: Counter[str] = Counter(_directory(n.id) for n in files)
    mapping = assign_components(file_dirs) if file_dirs else {}

    def component_of(node_id: str) -> str | None:
        return mapping.get(_directory(node_id))

    comp_edges: Counter[tuple[str, str]] = Counter()
    for e in resolved:
        assert e.target is not None
        a, b = component_of(e.source), component_of(e.target)
        if a is not None and b is not None and a != b:
            comp_edges[(a, b)] += 1

    disposition = "complete"
    file_cycles: tuple[tuple[str, ...], ...] = ()
    component_cycles: tuple[tuple[str, ...], ...] = ()
    layers: tuple[tuple[str, ...], ...] = ()
    layer_of: dict[str, int] = {}

    def projection(nodes: list[str], edges: list[tuple[str, str]], family: str):  # type: ignore[no-untyped-def]
        return build_projection(
            nodes,
            [GraphEdge(f"{s}>{t}", s, t) for s, t in edges],
            relation_family=family,
        )

    if file_ids:
        result = strong_components(
            projection(sorted(file_ids), sorted(file_edges), "imports"), budget=limits
        )
        if result.disposition != "complete":
            disposition = result.reason or result.disposition
        else:
            file_cycles = tuple(c for c in result.components if len(c) > 1)
    component_names = sorted(set(mapping.values()))
    if component_names:
        graph_c = projection(component_names, sorted(comp_edges), "component_imports")
        scc = strong_components(graph_c, budget=limits)
        cond = condense(graph_c, budget=limits)
        if scc.disposition != "complete" or cond.disposition != "complete":
            disposition = disposition if disposition != "complete" else (scc.reason or "budget")
        else:
            component_cycles = tuple(c for c in scc.components if len(c) > 1)
            # Longest-path layering on the condensation: a component sits above what it imports.
            depth_of = [0] * len(cond.components)
            outgoing: dict[int, list[int]] = defaultdict(list)
            for s, t in cond.edges:
                outgoing[s].append(t)
            for s in cond.topological_order:
                for t in outgoing[s]:
                    depth_of[t] = max(depth_of[t], depth_of[s] + 1)
            by_layer: dict[int, list[str]] = defaultdict(list)
            for index, members in enumerate(cond.components):
                for name in members:
                    layer_of[name] = depth_of[index]
                    by_layer[depth_of[index]].append(name)
            layers = tuple(tuple(sorted(by_layer[k])) for k in sorted(by_layer))

    languages: dict[str, Counter[str]] = defaultdict(Counter)
    file_count: Counter[str] = Counter()
    for n in files:
        c = component_of(n.id)
        if c is not None:
            file_count[c] += 1
            languages[c][n.language or "unknown"] += 1
    summaries = []
    for name in component_names:
        neighbours = sorted(
            ((b, n) for (a, b), n in comp_edges.items() if a == name), key=lambda x: (-x[1], x[0])
        )
        summaries.append(
            ComponentSummary(
                name=name,
                files=file_count[name],
                languages=tuple(sorted(languages[name].items(), key=lambda x: (-x[1], x[0]))),
                inbound=sum(n for (a, b), n in comp_edges.items() if b == name),
                outbound=sum(n for (a, b), n in comp_edges.items() if a == name),
                depends_on=tuple(neighbours[:5]),
                layer=layer_of.get(name),
            )
        )
    summaries.sort(key=lambda c: (-c.files, c.name))

    def top(counter: Counter[str]) -> tuple[Hub, ...]:
        ranked = sorted(counter.items(), key=lambda x: (-x[1], x[0]))[:TOP]
        return tuple(Hub(node, count) for node, count in ranked if count > 0)

    external_users: dict[str, set[str]] = defaultdict(set)
    for e in graph.edges:
        if e.resolution == "external" and e.target is not None:
            external_users[e.target].add(e.source)
    external = tuple(
        sorted(((k, len(v)) for k, v in external_users.items()), key=lambda x: (-x[1], x[0]))
    )
    entries = tuple(
        sorted(
            n.id
            for n in files
            if (n.label.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower() in ENTRY_NAMES)
            and indeg[n.id] == 0
        )
    )
    return Analytics(
        disposition=disposition,
        file_cycles=tuple(sorted(file_cycles)),
        component_cycles=tuple(sorted(component_cycles)),
        hubs_in=top(indeg),
        hubs_out=top(outdeg),
        components=tuple(summaries),
        layers=layers,
        entry_candidates=entries,
        external=external,
        counts={**graph.counts(), "files": len(files), "components": len(component_names)},
        component_by_file={n.id: c for n in files if (c := component_of(n.id)) is not None},
    )
