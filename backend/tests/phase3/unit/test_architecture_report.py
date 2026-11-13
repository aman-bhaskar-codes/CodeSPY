"""The assembled report: typed statements, honest unknowns, no source values, deterministic."""

import json
import random
from uuid import uuid4

from app.analyzers.graph import FileEntry
from app.analyzers.graph.render import graph_payload, render_markdown, statement_text
from app.analyzers.graph.report import LIMITATIONS, assemble_report

from tests.phase3.unit.test_dependency_graph import entries

SECRET = "hunter2-SUPER-SECRET-DEFAULT"  # noqa: S105 - must never appear in any report output

FILES = {
    "app/__init__.py": "",
    "app/web.py": (
        "import os\nfrom flask import Flask\nfrom app import models\nfrom app.util import helper\n"
        f'app = Flask(__name__)\nDB = os.getenv("DB_URL", "{SECRET}")\n'
        '@app.route("/users", methods=["GET"])\ndef users(): return helper()\n'
        '@app.route("/items")\ndef items(): return []\n'
    ),
    "app/models.py": (
        "from sqlalchemy import Column, Integer\nclass User(Base):\n"
        '    __tablename__ = "users"\n    id = Column(Integer, primary_key=True)\n'
    ),
    "app/util.py": "import ghost_internal\ndef helper(): return 1\n",
    "main.py": "from app import web\nimport requests\n",
}


def build(files=None, **overrides):
    items = entries(files or FILES)
    values = {
        "org_id": uuid4(),
        "repository_id": uuid4(),
        "snapshot_id": uuid4(),
        "files": items,
        "inventory": [f.path for f in items] + ["ghost_internal/x.py"],
        "eligible_files": len(items),
        "omitted_files": 0,
        "fact_runs": (uuid4(),),
    }
    return assemble_report(**{**values, **overrides}), items


def statements(report, name):
    return next(s for s in report.sections if s.name == name).statements


def test_sections_carry_typed_statements_tables_and_evidence():
    report, _ = build()
    names = [s.name for s in report.sections]
    assert names == [
        "overview", "components", "dependencies", "api_surface",
        "data_model", "configuration", "entry_points", "coverage",
    ]  # fmt: skip
    overview = statements(report, "overview")
    assert {s.code for s in overview} >= {"overview.files", "overview.symbols", "stack.framework"}
    framework = next(s for s in overview if s.code == "stack.framework")
    assert framework.kind == "inference" and framework.params["framework"] == "Flask"
    assert framework.evidence and framework.evidence[0].fact == "import"
    api = next(s for s in report.sections if s.name == "api_surface")
    assert next(s for s in api.statements if s.code == "api.routes").params["count"] == 2
    assert {row[2] for row in api.tables[0].rows} == {"/users", "/items"}
    data = next(s for s in report.sections if s.name == "data_model")
    assert data.tables[0].rows[0][1:3] == ("User", "users")
    config = next(s for s in report.sections if s.name == "configuration")
    assert ("env_var", "DB_URL", 1) in config.tables[0].rows


def test_what_the_facts_cannot_settle_is_stated_as_unknown_never_absent():
    report, _ = build()
    deps = {s.code: s for s in statements(report, "dependencies")}
    assert (
        deps["dependencies.unresolved"].kind == "unknown"
    )  # ghost_internal exists but is unlocatable
    assert deps["dependencies.unresolved"].evidence
    coverage = statements(report, "coverage")
    assert [s.code for s in coverage if s.code.startswith("limitation.")] == list(LIMITATIONS)
    assert all(s.kind == "unknown" for s in coverage if s.code.startswith("limitation."))
    assert report.coverage.complete and not any(s.code == "coverage.truncated" for s in coverage)


def test_truncated_coverage_is_loud_in_the_data_and_the_markdown():
    report, items = build(eligible_files=len(FILES) + 40, omitted_files=40)
    coverage = {s.code: s for s in statements(report, "coverage")}
    assert coverage["coverage.truncated"].kind == "unknown"
    assert not report.coverage.complete
    markdown = render_markdown(report)
    assert markdown.splitlines()[2].startswith("> **Partial coverage:**")
    assert f"{len(items)} of {len(items) + 40}" in markdown


def test_no_source_value_reaches_the_report_markdown_or_graph():
    report, _ = build()
    for text in (
        json.dumps(report.model_dump(mode="json")),
        render_markdown(report),
        json.dumps(graph_payload(report)),
    ):
        assert SECRET not in text
    assert "DB_URL" in render_markdown(report)  # names are recorded, values never


def test_report_is_deterministic_and_independent_of_file_order():
    first, items = build()
    ids = {
        "org_id": first.org_id,
        "repository_id": first.repository_id,
        "snapshot_id": first.snapshot_id,
        "fact_runs": first.coverage.fact_runs,
    }
    shuffled = list(items)
    random.Random(5).shuffle(shuffled)  # noqa: S311
    second = assemble_report(
        **ids,
        files=shuffled,
        inventory=[f.path for f in items] + ["ghost_internal/x.py"],
        eligible_files=len(items),
        omitted_files=0,
    )
    assert second.digest() == first.digest() and render_markdown(second) == render_markdown(first)
    assert isinstance(items[0], FileEntry)


def test_markdown_and_graph_payload_shape():
    report, _ = build()
    markdown = render_markdown(report)
    for heading in (
        "## Overview",
        "## Components",
        "## Dependencies",
        "## API surface",
        "## Coverage and limitations",
    ):
        assert heading in markdown
    assert "**INFERRED** Flask appears to be used" in markdown
    assert "**UNKNOWN**" in markdown and "| /users |" in markdown.replace(
        "GET | /users", "| /users"
    )
    payload = graph_payload(report)
    ids = {n["id"] for n in payload["nodes"]}
    assert all(link["source"] in ids and link["target"] in ids for link in payload["links"])
    assert payload["unknown"]["unresolved"] >= 1
    assert any(n["group"] for n in payload["nodes"] if n["kind"] == "file")
    assert statement_text(
        next(s for s in statements(report, "overview") if s.code == "overview.files")
    ).startswith("5 source files")


def test_every_statement_code_the_report_emits_has_a_template():
    from app.analyzers.graph.render import TEXT

    report, _ = build(eligible_files=len(FILES) + 5, omitted_files=5)
    codes = {s.code for section in report.sections for s in section.statements}
    assert codes <= set(TEXT), sorted(codes - set(TEXT))
    assert set(LIMITATIONS) <= set(TEXT)


def test_long_paths_and_external_names_do_not_break_a_build():
    long_dir = "d" * 300
    files = {
        f"{long_dir}/{long_dir}/index.py": "",
        "main.py": "import " + "a" * 1500 + "\n",
    }
    report, _ = build(files)
    assert report.sections  # clipped, not raised
    assert all(len(n.id) <= 1100 for n in report.graph.nodes)


def test_route_handlers_and_token_like_paths_are_not_echoed():
    files = {
        "app.js": (
            "const express = require('express');\nconst app = express();\n"
            "app.get('/hook/abcdef0123456789abcdef0123456789', auth('Bearer sk_live_ZZ9'));\n"
        )
    }
    report, _ = build(files)
    blob = json.dumps(report.model_dump(mode="json"))
    assert "sk_live_ZZ9" not in blob
    assert "abcdef0123456789abcdef0123456789" not in blob


def test_markdown_cannot_be_structured_by_control_characters():
    files = {"a\r# forged.py": "", "main.py": "import os\n"}
    report, _ = build(files)
    assert "\r" not in render_markdown(report)


def test_graph_pages_reuse_the_component_grouping(monkeypatch):
    from app.analyzers.graph import render

    report, _ = build()
    calls = []
    real = render.analyze
    monkeypatch.setattr(render, "analyze", lambda g: calls.append(1) or real(g))
    render._GROUPS.clear()
    first = render.graph_page(report, 0, 5)
    second = render.graph_page(report, 5, 5)
    assert len(calls) == 1 and first.nodes_total == second.nodes_total


def test_manifests_become_declared_observations_and_not_graph_nodes():
    files = {
        "go.mod": "module example.com/app\nrequire github.com/gin-gonic/gin v1.9.1\n",
        "bad/Cargo.toml": "[package",
        "main.go": 'package main\nimport "github.com/gin-gonic/gin"\n',
    }
    report, _ = build(files, inventory=list(files))
    deps = statements(report, "dependencies")
    declared = [s for s in deps if s.code == "manifest.declared"]
    assert len(declared) == 1 and declared[0].kind == "observation" and declared[0].evidence
    assert any(s.code == "manifest.unparsed" and s.kind == "unknown" for s in deps)
    assert all(n.language != "manifest" for n in report.graph.nodes)
    assert report.coverage.analyzed_files == 3 and report.coverage.partial_files == 1
    blob = json.dumps(report.model_dump(mode="json"))
    assert "v1.9.1" not in blob
    table = next(
        t
        for t in next(s for s in report.sections if s.name == "dependencies").tables
        if t.name == "declared_dependencies"
    )
    assert ("go.mod", "go", "github.com/gin-gonic/gin", "runtime") in table.rows
