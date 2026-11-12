"""Cycles, hubs, components and layers are derived only from resolved edges and are stable."""

import random
from collections import Counter

from app.analyzers.graph import build_graph
from app.analyzers.graph.analytics import analyze, assign_components
from app.core.dsa.puretypes import GraphBudget

from tests.phase3.unit.test_dependency_graph import entries


def graph_of(files):
    return build_graph(entries(files))


def test_file_cycles_hubs_and_entry_points():
    graph = graph_of(
        {
            "main.py": "import core\nimport a\n",
            "core.py": "",
            "a.py": "import b\nimport core\n",
            "b.py": "import a\nimport core\n",
        }
    )
    result = analyze(graph)
    assert result.disposition == "complete"
    assert result.file_cycles == (("file:a.py", "file:b.py"),)
    assert [(h.node, h.count) for h in result.hubs_in][:1] == [("file:core.py", 3)]
    assert result.hubs_out[0].node in {"file:a.py", "file:main.py", "file:b.py"}
    assert result.entry_candidates == (
        "file:main.py",
    )  # named like an entry and nothing imports it


def test_ambiguous_external_and_unresolved_edges_never_create_dependencies():
    graph = graph_of(
        {
            "x/main.py": "import util\nimport os\nimport ghost_internal\n",
            "x/util.py": "",
            "y/util.py": "",
            "z/other.py": "import util\n",
        }
    )
    result = analyze(graph)
    # `util` from x/main.py resolves to the closer x/util.py; from z/other.py it is ambiguous,
    # so z/other.py gains no dependency and x/util.py gains no extra importer.
    assert {h.node: h.count for h in result.hubs_in} == {"file:x/util.py": 1}
    assert ("ext:python:os", 1) in result.external


def test_components_collapse_single_child_chains_and_split_large_directories():
    sizes = Counter(
        {
            "src/main/java/org/ex/owner": 12,
            "src/main/java/org/ex/vet": 12,
            "src/main/java/org/ex/system": 12,
            "src/test/java/org/ex": 5,
            "docs": 1,
        }
    )
    mapping = assign_components(sizes)
    assert set(mapping.values()) == {
        "docs",
        "src/main/owner",
        "src/main/vet",
        "src/main/system",
        "src/test",
    }
    assert assign_components(sizes) == mapping  # stable


def test_component_cycles_and_layers():
    graph = graph_of(
        {
            "app/ui.py": "from lib import service\n",
            "lib/service.py": "from store import db\n",
            "store/db.py": "from lib import service\n",
            "app/__init__.py": "",
            "lib/__init__.py": "",
            "store/__init__.py": "",
        }
    )
    result = analyze(graph)
    names = {c.name: c for c in result.components}
    assert set(names) == {"app", "lib", "store"}
    assert result.component_cycles == (("lib", "store"),)
    assert names["app"].layer == 0 and names["lib"].layer == names["store"].layer == 1
    assert result.layers == (("app",), ("lib", "store"))
    assert names["app"].depends_on == (("lib", 2),)  # package file plus the named submodule


def test_output_is_independent_of_input_order_and_a_budget_stop_is_reported():
    files = {f"p{i % 3}/m{i}.py": f"import p{(i + 1) % 3}.m{(i + 3) % 9}\n" for i in range(9)}
    items = list(files.items())
    random.Random(3).shuffle(items)  # noqa: S311
    assert analyze(graph_of(files)) == analyze(graph_of(dict(items)))
    starved = analyze(graph_of(files), GraphBudget(max_nodes=1, max_edges=1))
    assert starved.disposition != "complete"  # an incomplete computation says so
