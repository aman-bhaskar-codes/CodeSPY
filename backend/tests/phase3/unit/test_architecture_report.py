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
