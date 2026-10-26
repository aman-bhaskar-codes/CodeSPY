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
from uuid import UUID, uuid4

import pytest
from app.contracts.investigation_execution import (
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
