"""Report contracts: typed statements, closed graphs, bounded and deterministic documents."""

from uuid import uuid4

import pytest
from app.contracts.architecture_report import (
    ArchitectureReportV1,
    CoverageV1,
    DependencyGraphV1,
    EvidenceRefV1,
    GraphEdgeV1,
    GraphNodeV1,
    SectionV1,
    StatementV1,
    TableV1,
    inputs_digest,
)
from app.domain.source_ranges import SourceRangeV1
from pydantic import ValidationError

RANGE = SourceRangeV1(start_byte=0, end_byte=10, start_line=1, end_line=1, start_col=0, end_col=10)


def evidence(path="a.py"):
    return EvidenceRefV1(file_id=uuid4(), path=path, fact="import", range=RANGE)


def node(id_, **kwargs):
    kind = kwargs.pop("kind", "file")
    return GraphNodeV1(id=id_, kind=kind, label=id_, **kwargs)


def edge(source, target, resolution, **kwargs):
    return GraphEdgeV1(
        source=source,
        target=target,
        module="m",
        import_kind="static",
        resolution=resolution,
        evidence=evidence(),
        **kwargs,
    )


def test_an_observation_needs_evidence_but_an_unknown_does_not():
    with pytest.raises(ValidationError):
        StatementV1(kind="observation", code="x.y")
    assert StatementV1(kind="unknown", code="imports.unresolved", params={"count": 3}).kind
    assert StatementV1(kind="observation", code="x.y", evidence=(evidence(),)).evidence_total == 1
    with pytest.raises(ValidationError):  # cannot claim fewer total than shown
        StatementV1(kind="derivation", code="x.y", evidence=(evidence(),), evidence_total=0)


@pytest.mark.parametrize(
    "code,params",
    [("Bad Code", {}), ("x.y", {"": 1}), ("x.y", {"k": "v" * 513})],
)
def test_codes_and_parameters_are_bounded(code, params):
    with pytest.raises(ValidationError):
        StatementV1(kind="unknown", code=code, params=params)


def test_edges_are_coherent_with_their_resolution():
    assert edge("a", "b", "resolved").target == "b"
    assert edge("a", None, "unresolved").candidates == ()
    assert edge("a", None, "ambiguous", candidates=("b", "c")).resolution == "ambiguous"
    for bad in (
        lambda: edge("a", None, "resolved"),  # resolved without a target
        lambda: edge("a", "b", "unresolved"),  # unresolved with a target
        lambda: edge("a", None, "ambiguous", candidates=("b",)),  # one candidate is not ambiguous
        lambda: edge("a", "b", "resolved", candidates=("b", "c")),  # candidates only if ambiguous
    ):
        with pytest.raises(ValidationError):
            bad()


def test_the_graph_is_closed_over_its_nodes():
    nodes = (node("a"), node("b"), node("ext:py:os", kind="external"))
    graph = DependencyGraphV1(
        nodes=nodes,
        edges=(
            edge("a", "b", "resolved"),
            edge("a", "ext:py:os", "external"),
            edge("a", None, "unresolved"),
        ),
    )
    assert graph.counts() == {"resolved": 1, "ambiguous": 0, "external": 1, "unresolved": 1}
    with pytest.raises(ValidationError):
        DependencyGraphV1(nodes=nodes, edges=(edge("a", "missing", "resolved"),))
    with pytest.raises(ValidationError):
        DependencyGraphV1(nodes=(node("a"), node("a")))


def test_tables_are_rectangular_and_coverage_is_consistent():
    TableV1(name="langs", columns=("language", "files"), rows=(("python", 3),), rows_total=5)
    with pytest.raises(ValidationError):
        TableV1(name="langs", columns=("a", "b"), rows=(("x",),))
    with pytest.raises(ValidationError):
        CoverageV1(eligible_files=2, analyzed_files=3, partial_files=0, omitted_files=0)
    assert CoverageV1(eligible_files=2, analyzed_files=2, partial_files=1, omitted_files=0).complete
    assert not CoverageV1(
        eligible_files=3, analyzed_files=2, partial_files=0, omitted_files=1
    ).complete


def report(**overrides):
    values = {
        "org_id": uuid4(),
        "repository_id": uuid4(),
        "snapshot_id": uuid4(),
        "inputs_digest": "a" * 64,
        "coverage": CoverageV1(
            eligible_files=1, analyzed_files=1, partial_files=0, omitted_files=0
        ),
        "graph": DependencyGraphV1(),
        "sections": (SectionV1(name="overview"),),
    }
    return ArchitectureReportV1(**{**values, **overrides})


def test_a_report_is_deterministic_and_rejects_duplicate_sections():
    ids = {"org_id": uuid4(), "repository_id": uuid4(), "snapshot_id": uuid4()}
    assert report(**ids).digest() == report(**ids).digest()
    assert report(**ids).digest() != report(**ids, inputs_digest="b" * 64).digest()
    with pytest.raises(ValidationError):
        report(sections=(SectionV1(name="overview"), SectionV1(name="overview")))
    report(**ids).check_size()


def test_inputs_digest_ignores_order_and_changes_with_content():
    a, b = uuid4(), uuid4()
    assert inputs_digest((a, b), ("x", "y")) == inputs_digest((b, a), ("y", "x"))
    assert inputs_digest((a, b), ("x", "y")) != inputs_digest((a, b), ("x", "z"))
