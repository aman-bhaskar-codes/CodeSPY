"""Unit tests for resumable investigation executor and coordinated role orchestration.

Verifies:
- Progression through logical nodes (gather -> investigate/plan -> verify -> ready_to_publish)
- Short-path vs two-specialist graph routing
- Restart/resume at node/model/tool boundaries with zero duplicate charges
- Step, tool, spending and context bounds enforcement
- Cancellation and deadline handling
- Model outcome unknown -> needs review
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from app.contracts.investigation_execution import (
    ClaimVerificationStatus,
    ClaimVerificationV1,
    InvestigationAdvanceStatus,
    InvestigationExecutionContextV1,
    InvestigationLimitsV1,
    InvestigationNodeKind,
    InvestigationScopeV1,
    InvestigationSpecV1,
    InvestigationStopReason,
    InvestigationToolName,
    InvestigationToolRequestV1,
    InvestigationToolResultV1,
    InvestigationToolStatus,
    compute_node_id,
)
from app.contracts.jobs import AttemptLeaseV2
from app.contracts.model import (
    ChatMessageV1,
    ModelResponseV1,
    TokenUsageV1,
    ToolCallV1,
)
from app.ports.investigation_execution import (
    InvestigationToolPort,
)
from app.services.investigation_executor import (
    InMemoryInvestigationAuthority,
    InMemoryInvestigationCheckpointRepo,
    InMemoryInvestigationStepReceiptRepo,
    InvestigationExecutorService,
)


def _make_response(
    content: str,
    *,
    tool_calls: tuple[ToolCallV1, ...] = (),
    charge_micros: int = 1000,
) -> ModelResponseV1:
    return ModelResponseV1(
        call_id=uuid4(),
        attempt_id=1,
        provider="synthetic",
        model_id="gpt-4o",
        content=content,
        tool_calls=tool_calls,
        usage=TokenUsageV1(input_tokens=100, output_tokens=50),
        charge_micros=charge_micros,
        finish_reason="tool_calls" if tool_calls else "stop",
    )


class DummyModelGateway:
    def __init__(self, responses: list[ModelResponseV1] | None = None) -> None:
        self.responses = list(responses or [])
        self.call_history: list[dict[str, Any]] = []

    async def execute_call(
        self,
        *,
        org_id: UUID,
        repository_id: UUID,
        principal_id: UUID,
        investigation_id: UUID,
        logical_call_id: UUID,
        attempt_id: int,
        messages: tuple[ChatMessageV1, ...],
        tools: tuple[Any, ...],
        requirement: Any,
        disclosure_policy: Any,
        idempotency_key: str,
    ) -> ModelResponseV1:
        self.call_history.append(
            {
                "logical_call_id": logical_call_id,
                "idempotency_key": idempotency_key,
                "messages": messages,
                "requirement": requirement,
            }
        )
        if self.responses:
            return self.responses.pop(0)
        return _make_response(
            (
                '{"verdict": "benign", "confidence": 0.9, '
                '"summary": "No vulnerability found.", "claims": []}'
            ),
            charge_micros=1000,
        )


class DummyToolService(InvestigationToolPort):
    def __init__(self) -> None:
        self.invocations: list[InvestigationToolRequestV1] = []
        self.evidence_to_produce: list[UUID] = []

    async def execute(
        self,
        request: InvestigationToolRequestV1,
        context: InvestigationExecutionContextV1,
    ) -> InvestigationToolResultV1:
        self.invocations.append(request)
        ev_id = uuid4()
        self.evidence_to_produce.append(ev_id)
        return InvestigationToolResultV1(
            status=InvestigationToolStatus.OK,
            payload="line 10: safe code",
            evidence_refs=(ev_id,),
            result_digest=hashlib.sha256(b"line 10: safe code").hexdigest(),
        )


def _make_spec(
    mode: str = "short_path",
    max_steps: int = 10,
    max_context_bytes: int = 32_768,
    budget_limit_micros: int = 5_000_000,
) -> InvestigationSpecV1:
    return InvestigationSpecV1(
        org_id=uuid4(),
        repository_id=uuid4(),
        snapshot_id=uuid4(),
        principal_id=uuid4(),
        target_type="repository",
        query="Check for SSRF vulnerability in url fetcher",
        mode=mode,  # type: ignore[arg-type]
        depth="standard",
        limits=InvestigationLimitsV1(
            max_steps=max_steps,
            max_context_bytes=max_context_bytes,
            budget_limit_micros=budget_limit_micros,
        ),
        disclosure_policy_digest="a" * 64,
        tool_policy_revision="v1",
        model_route_revision="v1",
        model_profile_revision="v1",
        runtime_revision=1,
        privacy_policy_revision="v1",
    )


def _make_context(
    spec: InvestigationSpecV1,
    investigation_id: UUID | None = None,
    deadline: datetime | None = None,
    execution_revision: int = 1,
) -> InvestigationExecutionContextV1:
    inv_id = investigation_id or uuid4()
    spec_hash = spec.compute_spec_hash()
    scope = InvestigationScopeV1(
        org_id=spec.org_id,
        repository_id=spec.repository_id,
        snapshot_id=spec.snapshot_id,
        principal_id=spec.principal_id,
        investigation_id=inv_id,
        spec_hash=spec_hash,
        execution_revision=execution_revision,
    )
    node_id = compute_node_id(
        spec.org_id, inv_id, spec_hash, InvestigationNodeKind.GATHER, "investigator", 0
    )
    return InvestigationExecutionContextV1(
        scope=scope,
        logical_node_id=node_id,
        node_kind=InvestigationNodeKind.GATHER,
        role="investigator",
        logical_ordinal=0,
        attempt_job_id=uuid4(),
        attempt_id=uuid4(),
        attempt_fence=1,
        deadline=deadline or (datetime.now(UTC) + timedelta(minutes=15)),
        limits=spec.limits,
    )


@pytest.mark.asyncio
async def test_advance_gather_creates_initial_state_short_path() -> None:
    spec = _make_spec(mode="short_path")
    ctx = _make_context(spec)

    authority = InMemoryInvestigationAuthority(ctx.scope)
    checkpoint_repo = InMemoryInvestigationCheckpointRepo()
    step_receipt_repo = InMemoryInvestigationStepReceiptRepo()
    gateway = DummyModelGateway()
    tools = DummyToolService()

    executor = InvestigationExecutorService(
        gateway=gateway,
        tools=tools,
        authority=authority,
        checkpoint_repo=checkpoint_repo,
        step_receipt_repo=step_receipt_repo,
        spec_provider=lambda s, o, h: spec,
    )

    advance = await executor.advance(ctx)
    assert advance.status == InvestigationAdvanceStatus.PROGRESS
    assert advance.checkpoint_sequence == 1

    latest = await checkpoint_repo.load_latest(None, ctx.scope)  # type: ignore[arg-type]
    assert latest is not None
    assert latest.state.next_node_kind == InvestigationNodeKind.INVESTIGATE
    assert latest.state.next_role == "investigator"
    assert latest.state.next_ordinal == 1


@pytest.mark.asyncio
async def test_advance_gather_two_specialist_routes_to_plan() -> None:
    spec = _make_spec(mode="two_specialist")
    ctx = _make_context(spec)

    authority = InMemoryInvestigationAuthority(ctx.scope)
    checkpoint_repo = InMemoryInvestigationCheckpointRepo()
    step_receipt_repo = InMemoryInvestigationStepReceiptRepo()
    gateway = DummyModelGateway()
    tools = DummyToolService()

    executor = InvestigationExecutorService(
        gateway=gateway,
        tools=tools,
        authority=authority,
        checkpoint_repo=checkpoint_repo,
        step_receipt_repo=step_receipt_repo,
        spec_provider=lambda s, o, h: spec,
    )

    advance = await executor.advance(ctx)
    assert advance.status == InvestigationAdvanceStatus.PROGRESS
    assert advance.checkpoint_sequence == 1

    latest = await checkpoint_repo.load_latest(None, ctx.scope)  # type: ignore[arg-type]
    assert latest is not None
    assert latest.state.next_node_kind == InvestigationNodeKind.PLAN
    assert latest.state.next_role == "planner"
    assert latest.state.next_ordinal == 1


@pytest.mark.asyncio
async def test_advance_investigate_executes_tools() -> None:
    spec = _make_spec(mode="short_path")
    ctx = _make_context(spec)

    authority = InMemoryInvestigationAuthority(ctx.scope)
    checkpoint_repo = InMemoryInvestigationCheckpointRepo()
    step_receipt_repo = InMemoryInvestigationStepReceiptRepo()

    # Step 1 model response has tool calls
    tool_resp = _make_response(
        content="I need to check the code for url handling.",
        tool_calls=(
            ToolCallV1(
                id="call-1",
                name="read_file_range",
                arguments_json=json.dumps(
                    {"path": "src/fetcher.py", "start_line": 1, "end_line": 50}
                ),
            ),
        ),
        charge_micros=2000,
    )
    gateway = DummyModelGateway([tool_resp])
    tools = DummyToolService()

    executor = InvestigationExecutorService(
        gateway=gateway,
        tools=tools,
        authority=authority,
        checkpoint_repo=checkpoint_repo,
        step_receipt_repo=step_receipt_repo,
        spec_provider=lambda s, o, h: spec,
    )

    # 1. Advance GATHER
    await executor.advance(ctx)

    # 2. Advance INVESTIGATE
    adv2 = await executor.advance(ctx)
    assert adv2.status == InvestigationAdvanceStatus.PROGRESS
    assert adv2.checkpoint_sequence == 2

    # Tool should have been called
    assert len(tools.invocations) == 1
    assert tools.invocations[0].tool_name == InvestigationToolName.READ_FILE_RANGE
    assert tools.invocations[0].arguments["path"] == "src/fetcher.py"

    latest = await checkpoint_repo.load_latest(None, ctx.scope)  # type: ignore[arg-type]
    assert latest is not None
    assert len(latest.state.committed_evidence_ids) == 1
    assert latest.state.usage_projection_micros == 2000
    assert latest.state.next_node_kind == InvestigationNodeKind.INVESTIGATE
    assert latest.state.next_ordinal == 2


@pytest.mark.asyncio
async def test_advance_investigate_terminal_response_transitions_to_verify() -> None:
    spec = _make_spec(mode="short_path")
    ctx = _make_context(spec)

    authority = InMemoryInvestigationAuthority(ctx.scope)
    checkpoint_repo = InMemoryInvestigationCheckpointRepo()
    step_receipt_repo = InMemoryInvestigationStepReceiptRepo()

    verdict_resp = _make_response(
        content=json.dumps(
            {
                "verdict": "benign",
                "confidence": 0.95,
                "summary": "Verified safe.",
                "claims": [
                    {
                        "claim_id": "c1",
                        "statement": "Fetcher validates scheme against whitelist",
                        "file_path": "src/fetcher.py",
                        "line_start": 10,
                        "line_end": 15,
                        "quoted_source": "if scheme not in ALLOWED: raise",
                    }
                ],
            }
        ),
        charge_micros=1500,
    )
    gateway = DummyModelGateway([verdict_resp])
    tools = DummyToolService()

    executor = InvestigationExecutorService(
        gateway=gateway,
        tools=tools,
        authority=authority,
        checkpoint_repo=checkpoint_repo,
        step_receipt_repo=step_receipt_repo,
        spec_provider=lambda s, o, h: spec,
    )

    await executor.advance(ctx)  # GATHER -> INVESTIGATE
    adv2 = await executor.advance(ctx)  # INVESTIGATE -> VERIFY
    assert adv2.status == InvestigationAdvanceStatus.PROGRESS

    latest = await checkpoint_repo.load_latest(None, ctx.scope)  # type: ignore[arg-type]
    assert latest is not None
    assert latest.state.next_node_kind == InvestigationNodeKind.VERIFY
    assert latest.state.next_role == "verifier"

    # Advance VERIFY -> READY_TO_PUBLISH
    adv3 = await executor.advance(ctx)
    assert adv3.status == InvestigationAdvanceStatus.READY_TO_PUBLISH


@pytest.mark.asyncio
async def test_step_budget_exhaustion_transitions_to_verify() -> None:
    spec = _make_spec(mode="short_path", max_steps=2)
    ctx = _make_context(spec)

    authority = InMemoryInvestigationAuthority(ctx.scope)
    checkpoint_repo = InMemoryInvestigationCheckpointRepo()
    step_receipt_repo = InMemoryInvestigationStepReceiptRepo()

    # Tool calls continuously
    tool_resp1 = _make_response(
        content="read 1",
        tool_calls=(
            ToolCallV1(
                id="call-1",
                name="read_file_range",
                arguments_json=json.dumps({"path": "src/a.py", "start_line": 1, "end_line": 10}),
            ),
        ),
        charge_micros=1000,
    )
    tool_resp2 = _make_response(
        content="read 2",
        tool_calls=(
            ToolCallV1(
                id="call-2",
                name="read_file_range",
                arguments_json=json.dumps({"path": "src/b.py", "start_line": 1, "end_line": 10}),
            ),
        ),
        charge_micros=1000,
    )
    gateway = DummyModelGateway([tool_resp1, tool_resp2])
    tools = DummyToolService()

    executor = InvestigationExecutorService(
        gateway=gateway,
        tools=tools,
        authority=authority,
        checkpoint_repo=checkpoint_repo,
        step_receipt_repo=step_receipt_repo,
        spec_provider=lambda s, o, h: spec,
    )

    await executor.advance(ctx)  # GATHER (ordinal 0 -> 1)
    await executor.advance(ctx)  # INVESTIGATE (ordinal 1 -> 2)
    adv = await executor.advance(ctx)  # Step limit 2 reached! Ordinal 2 -> VERIFY with stop reason
    assert adv.status == InvestigationAdvanceStatus.READY_TO_PUBLISH
    assert adv.stop_reason == InvestigationStopReason.STEP_BUDGET_EXHAUSTED


@pytest.mark.asyncio
async def test_spending_budget_exhaustion() -> None:
    spec = _make_spec(mode="short_path", budget_limit_micros=2000)
    ctx = _make_context(spec)

    authority = InMemoryInvestigationAuthority(ctx.scope)
    checkpoint_repo = InMemoryInvestigationCheckpointRepo()
    step_receipt_repo = InMemoryInvestigationStepReceiptRepo()

    expensive_resp = _make_response(
        content="read 1",
        tool_calls=(
            ToolCallV1(
                id="call-1",
                name="read_file_range",
                arguments_json=json.dumps({"path": "src/a.py", "start_line": 1, "end_line": 10}),
            ),
        ),
        charge_micros=2500,  # exceeds 2000 budget!
    )
    gateway = DummyModelGateway([expensive_resp])
    tools = DummyToolService()

    executor = InvestigationExecutorService(
        gateway=gateway,
        tools=tools,
        authority=authority,
        checkpoint_repo=checkpoint_repo,
        step_receipt_repo=step_receipt_repo,
        spec_provider=lambda s, o, h: spec,
    )

    await executor.advance(ctx)  # GATHER
    await executor.advance(ctx)  # INVESTIGATE -> hit budget limit!

    latest = await checkpoint_repo.load_latest(None, ctx.scope)  # type: ignore[arg-type]
    assert latest is not None
    assert latest.state.stop_reason == InvestigationStopReason.SPENDING_BUDGET_EXHAUSTED
    assert latest.state.next_node_kind == InvestigationNodeKind.VERIFY


@pytest.mark.asyncio
async def test_context_budget_exceeded() -> None:
    # Context bytes limit very tight: 100 bytes
    spec = _make_spec(mode="short_path", max_context_bytes=100)
    ctx = _make_context(spec)

    authority = InMemoryInvestigationAuthority(ctx.scope)
    checkpoint_repo = InMemoryInvestigationCheckpointRepo()
    step_receipt_repo = InMemoryInvestigationStepReceiptRepo()
    gateway = DummyModelGateway()
    tools = DummyToolService()

    executor = InvestigationExecutorService(
        gateway=gateway,
        tools=tools,
        authority=authority,
        checkpoint_repo=checkpoint_repo,
        step_receipt_repo=step_receipt_repo,
        spec_provider=lambda s, o, h: spec,
    )

    await executor.advance(ctx)  # GATHER
    adv = await executor.advance(ctx)  # Context bounds exceeded
    assert adv.status == InvestigationAdvanceStatus.TERMINAL_FAILURE
    assert adv.stop_reason == InvestigationStopReason.CONTEXT_BUDGET_EXCEEDED


@pytest.mark.asyncio
async def test_cancellation_halts_immediately() -> None:
    spec = _make_spec(mode="short_path")
    ctx = _make_context(spec)

    authority = InMemoryInvestigationAuthority(ctx.scope, is_cancelled=True)
    checkpoint_repo = InMemoryInvestigationCheckpointRepo()
    step_receipt_repo = InMemoryInvestigationStepReceiptRepo()
    gateway = DummyModelGateway()
    tools = DummyToolService()

    executor = InvestigationExecutorService(
        gateway=gateway,
        tools=tools,
        authority=authority,
        checkpoint_repo=checkpoint_repo,
        step_receipt_repo=step_receipt_repo,
        spec_provider=lambda s, o, h: spec,
    )

    adv = await executor.advance(ctx)
    assert adv.status == InvestigationAdvanceStatus.TERMINAL_FAILURE
    assert adv.stop_reason == InvestigationStopReason.CANCELLED


@pytest.mark.asyncio
async def test_deadline_exceeded_halts_immediately() -> None:
    spec = _make_spec(mode="short_path")
    # Expired deadline
    ctx = _make_context(spec, deadline=datetime.now(UTC) - timedelta(seconds=10))

    authority = InMemoryInvestigationAuthority(ctx.scope, is_expired=True)
    checkpoint_repo = InMemoryInvestigationCheckpointRepo()
    step_receipt_repo = InMemoryInvestigationStepReceiptRepo()
    gateway = DummyModelGateway()
    tools = DummyToolService()

    executor = InvestigationExecutorService(
        gateway=gateway,
        tools=tools,
        authority=authority,
        checkpoint_repo=checkpoint_repo,
        step_receipt_repo=step_receipt_repo,
        spec_provider=lambda s, o, h: spec,
    )

    adv = await executor.advance(ctx)
    assert adv.status == InvestigationAdvanceStatus.TERMINAL_FAILURE
    assert adv.stop_reason == InvestigationStopReason.DEADLINE_EXCEEDED


@pytest.mark.asyncio
async def test_two_specialist_lifecycle_routing() -> None:
    spec = _make_spec(mode="two_specialist", max_steps=10)
    ctx = _make_context(spec)

    authority = InMemoryInvestigationAuthority(ctx.scope)
    checkpoint_repo = InMemoryInvestigationCheckpointRepo()
    step_receipt_repo = InMemoryInvestigationStepReceiptRepo()

    planner_resp = _make_response(
        content="Hypothesis: SSRF exists in urllib.request", charge_micros=1000
    )
    evidence_resp = _make_response(content="Found url handler code", charge_micros=1000)
    auditor_resp = _make_response(
        content="Auditor counterexample: input is validated", charge_micros=1000
    )
    synthesizer_resp = _make_response(
        content=json.dumps(
            {"verdict": "benign", "confidence": 0.85, "summary": "Safe.", "claims": []}
        ),
        charge_micros=1000,
    )
    gateway = DummyModelGateway([planner_resp, evidence_resp, auditor_resp, synthesizer_resp])
    tools = DummyToolService()

    executor = InvestigationExecutorService(
        gateway=gateway,
        tools=tools,
        authority=authority,
        checkpoint_repo=checkpoint_repo,
        step_receipt_repo=step_receipt_repo,
        spec_provider=lambda s, o, h: spec,
    )

    # 1. GATHER -> PLAN
    await executor.advance(ctx)
    latest = await checkpoint_repo.load_latest(None, ctx.scope)  # type: ignore[arg-type]
    assert latest is not None
    assert latest.state.next_node_kind == InvestigationNodeKind.PLAN

    # 2. PLAN -> EVIDENCE
    await executor.advance(ctx)
    latest = await checkpoint_repo.load_latest(None, ctx.scope)  # type: ignore[arg-type]
    assert latest is not None
    assert latest.state.next_node_kind == InvestigationNodeKind.EVIDENCE

    # 3. EVIDENCE -> AUDIT
    await executor.advance(ctx)
    latest = await checkpoint_repo.load_latest(None, ctx.scope)  # type: ignore[arg-type]
    assert latest is not None
    assert latest.state.next_node_kind == InvestigationNodeKind.AUDIT

    # 4. AUDIT -> SYNTHESIZE
    await executor.advance(ctx)
    latest = await checkpoint_repo.load_latest(None, ctx.scope)  # type: ignore[arg-type]
    assert latest is not None
    assert latest.state.next_node_kind == InvestigationNodeKind.SYNTHESIZE

    # 5. SYNTHESIZE -> VERIFY
    await executor.advance(ctx)
    latest = await checkpoint_repo.load_latest(None, ctx.scope)  # type: ignore[arg-type]
    assert latest is not None
    assert latest.state.next_node_kind == InvestigationNodeKind.VERIFY

    # 6. VERIFY -> READY_TO_PUBLISH
    adv = await executor.advance(ctx)
    assert adv.status == InvestigationAdvanceStatus.READY_TO_PUBLISH

    # Verify task requirements routed to model
    task_types = [
        h["requirement"].task_type
        for h in gateway.call_history
        if hasattr(h["requirement"], "task_type")
    ]
    assert task_types == ["planner", "short_path", "auditor", "synthesizer"]


@pytest.mark.asyncio
async def test_resumable_restart_no_duplicate_charge() -> None:
    spec = _make_spec(mode="short_path")
    ctx = _make_context(spec)

    authority = InMemoryInvestigationAuthority(ctx.scope)
    checkpoint_repo = InMemoryInvestigationCheckpointRepo()
    step_receipt_repo = InMemoryInvestigationStepReceiptRepo()

    verdict_resp = _make_response(
        content=json.dumps(
            {"verdict": "benign", "confidence": 0.9, "summary": "Safe", "claims": []}
        ),
        charge_micros=1200,
    )
    gateway = DummyModelGateway([verdict_resp])
    tools = DummyToolService()

    executor = InvestigationExecutorService(
        gateway=gateway,
        tools=tools,
        authority=authority,
        checkpoint_repo=checkpoint_repo,
        step_receipt_repo=step_receipt_repo,
        spec_provider=lambda s, o, h: spec,
    )

    # Run gather
    await executor.advance(ctx)
    # Run investigate (commits receipt and moves cursor to VERIFY)
    await executor.advance(ctx)

    state_after_investigate = (await checkpoint_repo.load_latest(None, ctx.scope)).state  # type: ignore[union-attr]
    assert state_after_investigate.usage_projection_micros == 1200
    assert len(gateway.call_history) == 1

    # Simulate process crash / new executor instance resuming from existing checkpoint repo
    new_gateway = DummyModelGateway()
    new_executor = InvestigationExecutorService(
        gateway=new_gateway,
        tools=tools,
        authority=authority,
        checkpoint_repo=checkpoint_repo,
        step_receipt_repo=step_receipt_repo,
        spec_provider=lambda s, o, h: spec,
        journal=executor.journal,
    )

    # Resume from checkpoint -> next node is VERIFY -> finishes to READY_TO_PUBLISH
    adv = await new_executor.advance(ctx)
    assert adv.status == InvestigationAdvanceStatus.READY_TO_PUBLISH

    # No additional model calls made!
    assert len(new_gateway.call_history) == 0

    final_state = (await checkpoint_repo.load_latest(None, ctx.scope)).state  # type: ignore[union-attr]
    # Total spend did not double count
    assert final_state.usage_projection_micros == 1200


@pytest.mark.asyncio
async def test_uncertain_model_outcome_needs_review() -> None:
    spec = _make_spec(mode="short_path")
    ctx = _make_context(spec)

    authority = InMemoryInvestigationAuthority(ctx.scope)
    checkpoint_repo = InMemoryInvestigationCheckpointRepo()
    step_receipt_repo = InMemoryInvestigationStepReceiptRepo()
    gateway = DummyModelGateway()
    tools = DummyToolService()

    executor = InvestigationExecutorService(
        gateway=gateway,
        tools=tools,
        authority=authority,
        checkpoint_repo=checkpoint_repo,
        step_receipt_repo=step_receipt_repo,
        spec_provider=lambda s, o, h: spec,
    )

    # Run gather
    await executor.advance(ctx)

    # Record an uncertain step receipt for node 1
    node_id = compute_node_id(
        ctx.scope.org_id,
        ctx.scope.investigation_id,
        ctx.scope.spec_hash,
        InvestigationNodeKind.INVESTIGATE,
        "investigator",
        1,
    )
    await step_receipt_repo.record_receipt(
        session=None,  # type: ignore[arg-type]
        org_id=ctx.scope.org_id,
        investigation_id=ctx.scope.investigation_id,
        execution_revision=1,
        node_id=node_id,
        node_kind=InvestigationNodeKind.INVESTIGATE.value,
        role="investigator",
        logical_ordinal=1,
        input_digest="0" * 64,
        state="uncertain",
        attempt_id=ctx.attempt_id,
    )

    adv = await executor.advance(ctx)
    assert adv.status == InvestigationAdvanceStatus.NEEDS_REVIEW
    assert adv.stop_reason == InvestigationStopReason.MODEL_OUTCOME_UNKNOWN


@pytest.mark.asyncio
async def test_resume_returns_same_draft_with_model_claims():
    spec = _make_spec()
    ctx = _make_context(spec)
    identity = uuid4()
    response = _make_response(
        json.dumps(
            {
                "verdict": "indeterminate",
                "confidence": 0.4,
                "summary": "Needs evidence",
                "claims": [
                    {
                        "claim_id": str(identity),
                        "statement": "Potential issue",
                        "kind": "security_hypothesis",
                        "evidence_ids": [],
                    }
                ],
            }
        )
    )
    gateway = DummyModelGateway([response])
    executor = InvestigationExecutorService(
        gateway=gateway,
        tools=DummyToolService(),
        authority=InMemoryInvestigationAuthority(ctx.scope),
        checkpoint_repo=InMemoryInvestigationCheckpointRepo(),
        step_receipt_repo=InMemoryInvestigationStepReceiptRepo(),
        spec_provider=lambda s, o, h: spec,
    )
    await executor.advance(ctx)
    await executor.advance(ctx)
    ready = await executor.advance(ctx)
    assert ready.draft.claims[0].claim_id == identity
    assert (await executor.advance(ctx)).draft == ready.draft
    assert len(gateway.call_history) == 1


@pytest.mark.asyncio
async def test_followup_restores_tool_pair_and_continuation():
    spec = _make_spec()
    ctx = _make_context(spec)
    continuation = uuid4()
    response = _make_response(
        "",
        tool_calls=(
            ToolCallV1(
                id="read-1",
                name="read_file_range",
                arguments_json='{"file_path":"a.py","start_line":1,"end_line":1}',
            ),
        ),
    ).model_copy(update={"continuation_ref": continuation})
    gateway = DummyModelGateway([response, _make_response('{"claims":[]}')])
    executor = InvestigationExecutorService(
        gateway=gateway,
        tools=DummyToolService(),
        authority=InMemoryInvestigationAuthority(ctx.scope),
        checkpoint_repo=InMemoryInvestigationCheckpointRepo(),
        step_receipt_repo=InMemoryInvestigationStepReceiptRepo(),
        spec_provider=lambda s, o, h: spec,
    )
    for _ in range(3):
        await executor.advance(ctx)
    messages = gateway.call_history[1]["messages"]
    assert messages[-2].continuation_ref == continuation
    assert messages[-2].tool_calls == response.tool_calls
    assert messages[-1].tool_call_id == "read-1"
    assert "safe code" in messages[-1].content


@pytest.mark.asyncio
async def test_crash_after_tool_effect_replays_without_second_tool_or_model_call():
    spec = _make_spec()
    ctx = _make_context(spec)
    response = _make_response(
        "",
        tool_calls=(
            ToolCallV1(
                id="read-once",
                name="read_file_range",
                arguments_json='{"file_path":"a.py","start_line":1,"end_line":1}',
            ),
        ),
    )
    gateway, tools = DummyModelGateway([response]), DummyToolService()

    class InterruptedCheckpoints(InMemoryInvestigationCheckpointRepo):
        interrupted = False

        async def append(self, session, lease, *, expected_previous_sequence, state):
            if expected_previous_sequence == 1 and not self.interrupted:
                self.interrupted = True
                raise RuntimeError("simulated crash before checkpoint")
            return await super().append(
                session, lease, expected_previous_sequence=expected_previous_sequence, state=state
            )

    checkpoints = InterruptedCheckpoints()
    receipts = InMemoryInvestigationStepReceiptRepo()
    executor = InvestigationExecutorService(
        gateway=gateway,
        tools=tools,
        authority=InMemoryInvestigationAuthority(ctx.scope),
        checkpoint_repo=checkpoints,
        step_receipt_repo=receipts,
        spec_provider=lambda s, o, h: spec,
    )
    await executor.advance(ctx)
    with pytest.raises(RuntimeError, match="simulated crash"):
        await executor.advance(ctx)
    resumed = InvestigationExecutorService(
        gateway=gateway,
        tools=tools,
        authority=InMemoryInvestigationAuthority(ctx.scope),
        checkpoint_repo=checkpoints,
        step_receipt_repo=receipts,
        spec_provider=lambda s, o, h: spec,
        journal=executor.journal,
    )
    assert (await resumed.advance(ctx)).status == InvestigationAdvanceStatus.PROGRESS
    assert len(gateway.call_history) == len(tools.invocations) == 1
    assert checkpoints.checkpoints[-1].state.committed_evidence_ids == tuple(
        tools.evidence_to_produce
    )


@pytest.mark.asyncio
async def test_uncertain_gateway_outcome_requires_review_without_second_call():
    from app.services.configured_model_gateway import ModelConnectionError

    spec = _make_spec()
    ctx = _make_context(spec)

    class UncertainGateway(DummyModelGateway):
        async def execute_call(self, **kwargs):
            raise ModelConnectionError("model_call_pending_or_terminal")

    executor = InvestigationExecutorService(
        gateway=UncertainGateway(),
        tools=DummyToolService(),
        authority=InMemoryInvestigationAuthority(ctx.scope),
        checkpoint_repo=InMemoryInvestigationCheckpointRepo(),
        step_receipt_repo=InMemoryInvestigationStepReceiptRepo(),
        spec_provider=lambda s, o, h: spec,
    )
    await executor.advance(ctx)
    assert (await executor.advance(ctx)).status == InvestigationAdvanceStatus.NEEDS_REVIEW


@pytest.mark.asyncio
async def test_executor_replay_conflict_on_mismatched_input_digest() -> None:
    spec = _make_spec()
    ctx = _make_context(spec)
    gateway = DummyModelGateway([_make_response("test content")])
    executor = InvestigationExecutorService(
        gateway=gateway,
        tools=DummyToolService(),
        authority=InMemoryInvestigationAuthority(ctx.scope),
        checkpoint_repo=InMemoryInvestigationCheckpointRepo(),
        step_receipt_repo=InMemoryInvestigationStepReceiptRepo(),
        spec_provider=lambda s, o, h: spec,
    )
    # Advance GATHER -> INVESTIGATE
    await executor.advance(ctx)
    investigate_node_id = compute_node_id(
        ctx.scope.org_id,
        ctx.scope.investigation_id,
        ctx.scope.spec_hash,
        InvestigationNodeKind.INVESTIGATE,
        "investigator",
        1,
    )
    assert executor.journal is not None
    lease = executor.bound_lease or AttemptLeaseV2(
        org_id=ctx.scope.org_id,
        job_id=ctx.attempt_job_id,
        attempt_id=ctx.attempt_id,
        fence=ctx.attempt_fence,
        owner="executor",
        lease_expires_at=ctx.deadline,
        heartbeat_at=ctx.deadline,
    )
    # Pre-seed conflicting effect with different input_digest
    await executor.journal.put(
        ctx.scope, lease, investigate_node_id, "model", "f" * 64, {"content": "conflicting"}
    )
    # Advance INVESTIGATE - encounters replay conflict and terminates cleanly
    adv = await executor.advance(ctx)
    assert adv.status == InvestigationAdvanceStatus.TERMINAL_FAILURE
    assert adv.stop_reason == InvestigationStopReason.NONRESUMABLE
    assert "restart required" in (adv.message or "").lower()


@pytest.mark.asyncio
async def test_executor_reconstructs_prior_tool_results_into_next_turn() -> None:
    spec = _make_spec()
    ctx = _make_context(spec)
    resp1 = _make_response(
        "",
        tool_calls=(
            ToolCallV1(
                id="call-1",
                name="read_file_range",
                arguments_json='{"file_path":"main.py","start_line":1,"end_line":10}',
            ),
        ),
    )
    resp2 = _make_response(
        json.dumps({"verdict": "benign", "confidence": 0.8, "summary": "clean", "claims": []})
    )
    gateway = DummyModelGateway([resp1, resp2])
    tools = DummyToolService()
    executor = InvestigationExecutorService(
        gateway=gateway,
        tools=tools,
        authority=InMemoryInvestigationAuthority(ctx.scope),
        checkpoint_repo=InMemoryInvestigationCheckpointRepo(),
        step_receipt_repo=InMemoryInvestigationStepReceiptRepo(),
        spec_provider=lambda s, o, h: spec,
    )
    # 1. GATHER -> INVESTIGATE
    await executor.advance(ctx)
    # 2. INVESTIGATE: calls model (resp1 with tool call) -> executes tool -> next node
    adv2 = await executor.advance(ctx)
    assert adv2.status == InvestigationAdvanceStatus.PROGRESS
    # 3. Next turn: calls model with history of prior turn
    adv3 = await executor.advance(ctx)
    assert adv3.status == InvestigationAdvanceStatus.PROGRESS
    # Check the messages in the second model call
    assert len(gateway.call_history) == 2
    second_call_msgs = gateway.call_history[1]["messages"]
    # Verify assistant tool_call message and tool result message are present in history
    tool_call_msgs = [m for m in second_call_msgs if m.role == "assistant" and m.tool_calls]
    assert len(tool_call_msgs) == 1
    assert tool_call_msgs[0].tool_calls[0].id == "call-1"
    tool_resp_msgs = [m for m in second_call_msgs if m.role == "tool"]
    assert len(tool_resp_msgs) == 1
    assert tool_resp_msgs[0].tool_call_id == "call-1"


@pytest.mark.asyncio
async def test_executor_verify_step_delivers_structured_claims_to_verifier() -> None:
    spec = _make_spec()
    ctx = _make_context(spec)
    claim_data = {
        "claim_id": str(uuid4()),
        "statement": "Vulnerability exists in auth function",
        "kind": "security_hypothesis",
        "evidence_ids": [],
    }
    model_content = json.dumps(
        {
            "verdict": "indeterminate",
            "confidence": 0.5,
            "summary": "Hypothesis requires evidence",
            "claims": [claim_data],
        }
    )
    gateway = DummyModelGateway([_make_response(model_content)])
    tools = DummyToolService()
    from app.services.investigation_verification import (
        InvestigationVerificationService,
        VerificationOutcomeV1,
    )

    verifier_mock = AsyncMock()
    verifier_mock.verify_claims = AsyncMock(
        return_value=VerificationOutcomeV1(
            total_claims=1,
            confirmed_count=0,
            refuted_count=0,
            unsupported_count=1,
            grounding_ratio=0.0,
            is_grounded=False,
            adjusted_verdict="indeterminate",
            adjusted_confidence=0.5,
            summary="Verified indeterminate",
            verifications=(
                ClaimVerificationV1(
                    claim_id=UUID(claim_data["claim_id"]),
                    status=ClaimVerificationStatus.UNSUPPORTED,
                    notes="no evidence",
                ),
            ),
        )
    )
    real_verifier = InvestigationVerificationService()
    verifier_mock.prepare_draft = real_verifier.prepare_draft

    executor = InvestigationExecutorService(
        gateway=gateway,
        tools=tools,
        authority=InMemoryInvestigationAuthority(ctx.scope),
        checkpoint_repo=InMemoryInvestigationCheckpointRepo(),
        step_receipt_repo=InMemoryInvestigationStepReceiptRepo(),
        spec_provider=lambda s, o, h: spec,
        verification_service=verifier_mock,
    )
    # 1. GATHER -> INVESTIGATE
    await executor.advance(ctx)
    # 2. INVESTIGATE -> VERIFY
    await executor.advance(ctx)
    # 3. VERIFY -> READY_TO_PUBLISH
    adv = await executor.advance(ctx)
    assert adv.status == InvestigationAdvanceStatus.READY_TO_PUBLISH
    assert adv.draft is not None
    # Verify verifier was called with the structured claim from model output
    assert verifier_mock.verify_claims.called
    call_args = verifier_mock.verify_claims.call_args[1]
    claims = call_args["claims"]
    assert len(claims) == 1
    assert str(claims[0].claim_id) == claim_data["claim_id"]
    assert claims[0].statement == claim_data["statement"]
    # And draft report has the claims preserved
    assert len(adv.draft.claims) == 1
    assert str(adv.draft.claims[0].claim_id) == claim_data["claim_id"]


async def test_duplicate_tool_call_ids_fail_before_any_tool_executes():
    spec = _make_spec()
    ctx = _make_context(spec)
    call = ToolCallV1(id="duplicate", name="read_file_range", arguments_json="{}")
    gateway = DummyModelGateway([_make_response("", tool_calls=(call, call))])
    tools = DummyToolService()
    executor = InvestigationExecutorService(
        gateway=gateway,
        tools=tools,
        authority=InMemoryInvestigationAuthority(ctx.scope),
        checkpoint_repo=InMemoryInvestigationCheckpointRepo(),
        step_receipt_repo=InMemoryInvestigationStepReceiptRepo(),
        spec_provider=lambda s, o, h: spec,
    )
    await executor.advance(ctx)
    assert (await executor.advance(ctx)).status == InvestigationAdvanceStatus.TERMINAL_FAILURE
    assert tools.invocations == []
