"""Contract tests for MCP input arguments, output schemas, and forbidden identity rejection."""

from uuid import uuid4

import pytest
from app.contracts.mcp import (
    AssembleContextInputV1,
    GetEvidenceInputV1,
    GetIndexStatusInputV1,
    GetInvestigationResultInputV1,
    GetInvestigationStatusInputV1,
    GetSourceViewInputV1,
    GraphNeighborsInputV1,
    GraphNeighborsResultV1,
    ListClaimsInputV1,
    ListClaimsResultV1,
    ListFindingsInputV1,
    ListFindingsResultV1,
    McpClaimItemV1,
    McpEvidenceResultV1,
    McpFindingItemV1,
    McpGraphEdgeV1,
    McpGraphNodeV1,
    QueryMemoryInputV1,
    SearchExactInputV1,
    SearchHybridInputV1,
    TraceFeatureInputV1,
)
from pydantic import ValidationError

INPUT_CLASSES = [
    (GetIndexStatusInputV1, {"repository_id": uuid4(), "snapshot_id": uuid4(), "job_id": uuid4()}),
    (GetSourceViewInputV1, {"repository_id": uuid4(), "snapshot_id": uuid4(), "file_id": uuid4()}),
    (SearchExactInputV1, {"repository_id": uuid4(), "snapshot_id": uuid4(), "query": "auth"}),
    (SearchHybridInputV1, {"repository_id": uuid4(), "snapshot_id": uuid4(), "query": "auth"}),
    (AssembleContextInputV1, {"repository_id": uuid4(), "snapshot_id": uuid4(), "query": "auth"}),
    (QueryMemoryInputV1, {"repository_id": uuid4(), "snapshot_id": uuid4(), "query": "auth"}),
    (GetInvestigationStatusInputV1, {"repository_id": uuid4(), "investigation_id": uuid4()}),
    (GetInvestigationResultInputV1, {"repository_id": uuid4(), "investigation_id": uuid4()}),
    (ListFindingsInputV1, {"repository_id": uuid4(), "snapshot_id": uuid4(), "run_id": uuid4()}),
    (
        GetEvidenceInputV1,
        {"repository_id": uuid4(), "snapshot_id": uuid4(), "evidence_id": uuid4()},
    ),
    (GraphNeighborsInputV1, {"repository_id": uuid4(), "snapshot_id": uuid4(), "node_id": uuid4()}),
    (
        TraceFeatureInputV1,
        {"repository_id": uuid4(), "snapshot_id": uuid4(), "finding_id": uuid4()},
    ),
    (ListClaimsInputV1, {"repository_id": uuid4(), "investigation_id": uuid4()}),
]


@pytest.mark.parametrize("cls,valid_kwargs", INPUT_CLASSES)
def test_all_mcp_input_contracts_reject_identity_injection(cls, valid_kwargs):
    forbidden_keys = ["org_id", "principal_id", "role", "user_id", "credentials", "token"]
    for key in forbidden_keys:
        bad_kwargs = dict(valid_kwargs)
        bad_kwargs[key] = str(uuid4())
        with pytest.raises(ValidationError) as exc:
            cls.model_validate(bad_kwargs)
        assert "Tool arguments cannot supply identity or credential fields" in str(exc.value)


@pytest.mark.parametrize("cls,valid_kwargs", INPUT_CLASSES)
def test_all_mcp_input_contracts_forbid_extra_fields(cls, valid_kwargs):
    bad_kwargs = dict(valid_kwargs)
    bad_kwargs["unrecognized_extra_arg"] = "unexpected"
    with pytest.raises(ValidationError) as exc:
        cls.model_validate(bad_kwargs)
    assert "extra_forbidden" in str(exc.value)


def test_get_source_view_byte_range_validation():
    repo = uuid4()
    snap = uuid4()
    file_id = uuid4()

    # Valid both None
    v = GetSourceViewInputV1(repository_id=repo, snapshot_id=snap, file_id=file_id)
    assert v.start_byte is None and v.end_byte is None

    # Valid range
    v2 = GetSourceViewInputV1(
        repository_id=repo, snapshot_id=snap, file_id=file_id, start_byte=10, end_byte=100
    )
    assert v2.start_byte == 10 and v2.end_byte == 100

    # Start without end
    with pytest.raises(ValidationError) as exc:
        GetSourceViewInputV1(repository_id=repo, snapshot_id=snap, file_id=file_id, start_byte=10)
    assert "Byte range requires both start_byte and end_byte" in str(exc.value)

    # Inverted range
    with pytest.raises(ValidationError) as exc:
        GetSourceViewInputV1(
            repository_id=repo, snapshot_id=snap, file_id=file_id, start_byte=100, end_byte=10
        )
    assert "start_byte cannot exceed end_byte" in str(exc.value)


def test_search_and_memory_limit_bounds():
    repo = uuid4()
    snap = uuid4()

    # Search exact limit
    with pytest.raises(ValidationError):
        SearchExactInputV1(repository_id=repo, snapshot_id=snap, query="test", limit=0)
    with pytest.raises(ValidationError):
        SearchExactInputV1(repository_id=repo, snapshot_id=snap, query="test", limit=11)

    # Empty query rejected
    with pytest.raises(ValidationError):
        SearchExactInputV1(repository_id=repo, snapshot_id=snap, query="")


def test_output_projections_serialization_roundtrip():
    inv_id = uuid4()
    repo_id = uuid4()
    snap_id = uuid4()
    run_id = uuid4()
    claim_id = uuid4()

    # Claims result
    claim = McpClaimItemV1(
        claim_id=claim_id,
        statement="Auth bypass possible via null token",
        kind="vulnerability",
        status="source_correspondence_confirmed",
        evidence_refs=(uuid4(),),
        resolved_file="auth.py",
        resolved_line_start=12,
        resolved_line_end=15,
        notes="Grounded in code",
    )
    claims_res = ListClaimsResultV1(
        repository_id=repo_id,
        investigation_id=inv_id,
        claims=(claim,),
        total_count=1,
    )
    assert claims_res.claims[0].claim_id == claim_id

    # Findings result
    finding = McpFindingItemV1(
        id=uuid4(),
        rule_id="RULE-01",
        category="security",
        severity="high",
        disposition="supported-static",
        current_support="current_support",
        title="SQL injection in query handler",
        file_path="db.py",
        line=42,
        triage_state="confirmed",
    )
    findings_res = ListFindingsResultV1(
        repository_id=repo_id,
        snapshot_id=snap_id,
        run_id=run_id,
        findings=(finding,),
        next_cursor=None,
        total_count=1,
    )
    assert findings_res.findings[0].rule_id == "RULE-01"

    # Evidence result
    ev_id = uuid4()
    evidence_res = McpEvidenceResultV1(
        id=ev_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        run_id=run_id,
        file_path="server.py",
        observation_mode="static",
        basis="observed",
        coverage_status="complete",
        payload={"snippet": "line of code"},
        limitations=("bounded",),
    )
    assert evidence_res.id == ev_id

    # Graph neighbors result
    node_id = uuid4()
    node = McpGraphNodeV1(
        id=node_id, kind="function", label="authenticate", file_path="auth.py", line=10
    )
    edge = McpGraphEdgeV1(source_id=node_id, target_id=uuid4(), edge_kind="calls")
    graph_res = GraphNeighborsResultV1(
        repository_id=repo_id,
        snapshot_id=snap_id,
        structural_run_id=run_id,
        root_node_id=node_id,
        nodes=(node,),
        edges=(edge,),
        hop_count=1,
    )
    assert graph_res.root_node_id == node_id
