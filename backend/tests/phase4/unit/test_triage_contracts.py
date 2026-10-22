"""Unit tests for human triage and feature trace contracts."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from app.contracts.trace import FeatureTraceV1, TraceBudgetV1, TraceEdgeV1, TraceNodeV1
from app.contracts.triage import (
    TriageHistoryItemV1,
    TriageHistoryV1,
    TriageRecordV1,
    TriageTransitionV1,
)
from pydantic import ValidationError


def test_triage_transition_valid_unreviewed() -> None:
    t = TriageTransitionV1(state="unreviewed", reason="none", expected_version=0)
    assert t.state == "unreviewed"
    assert t.reason == "none"
    assert t.expected_version == 0


def test_triage_transition_valid_confirmed() -> None:
    t = TriageTransitionV1(state="confirmed", expected_version=1)
    assert t.state == "confirmed"
    assert t.reason == "none"


def test_triage_transition_requires_reason_for_false_positive() -> None:
    with pytest.raises(ValidationError, match="reason must be provided"):
        TriageTransitionV1(state="false_positive", reason="none", expected_version=1)

    t = TriageTransitionV1(state="false_positive", reason="test_code", expected_version=1)
    assert t.state == "false_positive"
    assert t.reason == "test_code"


def test_triage_transition_requires_reason_for_mitigated() -> None:
    with pytest.raises(ValidationError, match="reason must be provided"):
        TriageTransitionV1(state="mitigated", reason="none", expected_version=2)

    t = TriageTransitionV1(state="mitigated", reason="compensating_control", expected_version=2)
    assert t.state == "mitigated"


def test_triage_transition_requires_reason_for_accepted_risk() -> None:
    with pytest.raises(ValidationError, match="reason must be provided"):
        TriageTransitionV1(state="accepted_risk", reason="none", expected_version=1)

    t = TriageTransitionV1(state="accepted_risk", reason="business_context", expected_version=1)
    assert t.state == "accepted_risk"


def test_triage_transition_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        TriageTransitionV1.model_validate(
            {"state": "confirmed", "reason": "none", "expected_version": 0, "free_text": "leak"}
        )


def test_triage_record_and_history_models() -> None:
    finding_id = uuid4()
    event_id = uuid4()
    principal_id = uuid4()
    now = datetime.now(UTC)

    item = TriageHistoryItemV1(
        event_id=event_id,
        finding_id=finding_id,
        principal_id=principal_id,
        state="confirmed",
        reason="none",
        created_at=now,
    )
    history = TriageHistoryV1(finding_id=finding_id, events=[item])
    assert history.finding_id == finding_id
    assert len(history.events) == 1

    record = TriageRecordV1(
        finding_id=finding_id,
        current_state="confirmed",
        current_reason="none",
        last_event_id=event_id,
        updated_by=principal_id,
        updated_at=now,
        version=1,
    )
    assert record.version == 1


def test_feature_trace_contract() -> None:
    finding_id = uuid4()
    n1_id = uuid4()
    n2_id = uuid4()
    node1 = TraceNodeV1(
        id=n1_id, kind="finding", label="Hardcoded Secret", file_path="app/config.py", line=12
    )
    node2 = TraceNodeV1(id=n2_id, kind="route", label="GET /api/v1/auth")
    edge = TraceEdgeV1(source_id=n1_id, target_id=n2_id, edge_kind="route_handler")

    trace = FeatureTraceV1(
        finding_id=finding_id,
        nodes=[node1, node2],
        edges=[edge],
        hop_count=1,
        budget_exceeded=False,
    )
    assert trace.finding_id == finding_id
    assert len(trace.nodes) == 2
    assert len(trace.edges) == 1
    assert trace.nodes[0].file_path == "app/config.py"

    budget = TraceBudgetV1()
    assert budget.max_hops == 2
    assert budget.max_nodes == 100
    assert budget.max_edges == 200
    assert budget.timeout_seconds == 1.0
