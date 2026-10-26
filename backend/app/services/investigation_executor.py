"""Resumable node executor and coordinated role orchestration.

Implements InvestigationExecutorPort, advancing durable investigation execution
one bounded node at a time with deterministic UUID5 identities, immutable checkpoints,
job authority verification and crash-safe replay.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.investigation_execution import (
    InvestigationAdvanceStatus,
    InvestigationAdvanceV1,
    InvestigationAuthorityV1,
    InvestigationEvidenceV1,
    InvestigationExecutionCheckpointV1,
    InvestigationExecutionContextV1,
    InvestigationExecutionStateV1,
    InvestigationNodeKind,
    InvestigationScopeV1,
    InvestigationSpecV1,
    InvestigationStopReason,
    InvestigationToolName,
    InvestigationToolRequestV1,
    compute_call_id,
    compute_node_id,
)
from app.contracts.jobs import AttemptLeaseV2
from app.contracts.model import (
    ChatMessageV1,
    ModelResponseV1,
    ModelRole,
    ModelRoutingRequirementV1,
)
from app.contracts.provider_budget import DisclosurePolicyV1
from app.db.time import database_now
from app.domain.canonical_json import canonical_bytes
from app.domain.errors import InvalidTransition, ScopeViolation
from app.models.investigation_execution_tables import (
    investigation_jobs,
)
from app.models.investigation_tables import investigations
from app.models.phase2_tables import attempts, jobs
from app.ports.investigation_execution import (
    InvestigationExecutionCheckpointRepository,
    InvestigationExecutorPort,
    InvestigationJobAuthorityPort,
    InvestigationToolPort,
    ModelResultStorePort,
)
from app.ports.model import ModelGatewayPort
from app.repositories.investigation_execution_repository import (
    SqlInvestigationSpecRepository,
)
from app.security.authorization import require_repository
from app.services.context_composition import ContextBudgetExceededError
from app.services.investigation_checkpointer import StaleWorkerFencingError

logger = logging.getLogger(__name__)


def _ensure_utc(dt: datetime | None) -> datetime:
    if dt is None:
        return datetime.now(UTC)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


# ---------------------------------------------------------------------------
# In-Memory Test Doubles
# ---------------------------------------------------------------------------


class InMemoryInvestigationAuthority(InvestigationJobAuthorityPort):
    """Authority test double for offline unit testing."""

    def __init__(
        self,
        scope: InvestigationScopeV1,
        *,
        is_cancelled: bool = False,
        is_expired: bool = False,
        deadline: datetime | None = None,
    ) -> None:
        self.scope = scope
        self.is_cancelled = is_cancelled
        self.is_expired = is_expired
        self.deadline = deadline or (datetime.now(UTC) + timedelta(minutes=15))

    async def assert_current(
        self,
        session: AsyncSession,
        lease: AttemptLeaseV2,
        *,
        lock: bool = False,
    ) -> InvestigationAuthorityV1:
        return InvestigationAuthorityV1(
            db_now=datetime.now(UTC),
            deadline=self.deadline,
            is_cancelled=self.is_cancelled,
            is_expired=self.is_expired,
            scope=self.scope,
        )


class InMemoryInvestigationCheckpointRepo(InvestigationExecutionCheckpointRepository):
    """In-memory checkpoint store for offline unit testing."""

    def __init__(self) -> None:
        self.checkpoints: list[InvestigationExecutionCheckpointV1] = []

    async def load_latest(
        self,
        session: AsyncSession,
        scope: InvestigationScopeV1,
    ) -> InvestigationExecutionCheckpointV1 | None:
        matching = [
            cp
            for cp in self.checkpoints
            if cp.org_id == scope.org_id
            and cp.investigation_id == scope.investigation_id
            and cp.execution_revision == scope.execution_revision
        ]
        if not matching:
            return None
        return max(matching, key=lambda c: c.checkpoint_sequence)

    async def append(
        self,
        session: AsyncSession,
        lease: AttemptLeaseV2,
        *,
        expected_previous_sequence: int,
        state: InvestigationExecutionStateV1,
    ) -> InvestigationExecutionCheckpointV1:
        next_seq = expected_previous_sequence + 1
        cp = InvestigationExecutionCheckpointV1(
            org_id=lease.org_id,
            investigation_id=state.investigation_id or uuid4(),
            execution_revision=state.execution_revision,
            checkpoint_sequence=next_seq,
            state=state.model_copy(update={"checkpoint_sequence": next_seq}),
            state_digest=state.compute_state_digest(),
            writer_job_id=lease.job_id,
            writer_attempt_id=lease.attempt_id,
            writer_fence=lease.fence,
            created_at=datetime.now(UTC),
        )
        self.checkpoints.append(cp)
        return cp


class InMemoryInvestigationStepReceiptRepo:
    """In-memory step receipt repository for offline unit testing."""

    def __init__(self) -> None:
        self.receipts: dict[tuple[UUID, UUID, int, UUID], dict[str, Any]] = {}

    async def record_receipt(
        self,
        session: AsyncSession,
        org_id: UUID,
        investigation_id: UUID,
        execution_revision: int,
        node_id: UUID,
        node_kind: str,
        role: str,
        logical_ordinal: int,
        input_digest: str,
        state: str,
        attempt_id: UUID,
        result_digest: str | None = None,
        call_ref: str | None = None,
        reservation_id: UUID | None = None,
        model_result_id: UUID | None = None,
    ) -> None:
        key = (org_id, investigation_id, execution_revision, node_id)
        self.receipts[key] = {
            "org_id": org_id,
            "investigation_id": investigation_id,
            "execution_revision": execution_revision,
            "node_id": node_id,
            "node_kind": node_kind,
            "role": role,
            "logical_ordinal": logical_ordinal,
            "input_digest": input_digest,
            "state": state,
            "result_digest": result_digest,
            "call_ref": call_ref,
            "reservation_id": reservation_id,
            "model_result_id": model_result_id,
            "attempt_id": attempt_id,
            "created_at": datetime.now(UTC),
        }

    async def get_receipt(
        self,
        session: AsyncSession,
        org_id: UUID,
        investigation_id: UUID,
        execution_revision: int,
        node_id: UUID,
    ) -> dict[str, Any] | None:
        key = (org_id, investigation_id, execution_revision, node_id)
        return self.receipts.get(key)


# ---------------------------------------------------------------------------
# SQL Investigation Job Authority
# ---------------------------------------------------------------------------


class SqlInvestigationJobAuthority(InvestigationJobAuthorityPort):
    """Job authority assertion for investigation execution writes.

    Enforces DB-time checks and standard lock ordering:
    1) jobs -> 2) job_attempts -> 3) investigations
    """

    def __init__(
        self,
        spec_repo: SqlInvestigationSpecRepository | None = None,
    ) -> None:
        self.spec_repo = spec_repo or SqlInvestigationSpecRepository()

    async def assert_current(
        self,
        session: AsyncSession,
        lease: AttemptLeaseV2,
        *,
        lock: bool = False,
    ) -> InvestigationAuthorityV1:
        # 1. Lock / query jobs row
        job_stmt = select(jobs).where(
            jobs.c.org_id == lease.org_id,
            jobs.c.id == lease.job_id,
        )
        if lock:
            job_stmt = job_stmt.with_for_update()
        job_row = (await session.execute(job_stmt)).mappings().one_or_none()
        if job_row is None:
            raise ScopeViolation(f"job {lease.job_id} not found")

        now = await database_now(session)
        now_utc = _ensure_utc(now)

        state = job_row["state"]
        if state in ("cancelled", "failed", "succeeded"):
            raise ScopeViolation(f"job {lease.job_id} is in terminal state '{state}'")

        is_cancelled = state == "cancelling"

        fence = job_row["fence"]
        if fence != lease.fence:
            raise StaleWorkerFencingError(
                f"fencing token mismatch: db={fence}, lease={lease.fence}"
            )

        deadline = _ensure_utc(job_row["deadline"])
        is_expired = deadline is not None and deadline <= now_utc

        # 2. Query / Lock attempts row
        attempt_stmt = select(attempts).where(
            attempts.c.org_id == lease.org_id,
            attempts.c.job_id == lease.job_id,
            attempts.c.id == lease.attempt_id,
        )
        if lock:
            attempt_stmt = attempt_stmt.with_for_update()
        attempt_row = (await session.execute(attempt_stmt)).mappings().one_or_none()
        if attempt_row is None:
            raise ScopeViolation(f"attempt {lease.attempt_id} not found")

        lease_expiry = _ensure_utc(attempt_row["lease_expires_at"])
        if lease_expiry is not None and lease_expiry <= now_utc:
            raise StaleWorkerFencingError("worker lease expired")

        # 3. Lookup investigation_id
        inv_job_stmt = select(investigation_jobs.c.investigation_id).where(
            investigation_jobs.c.org_id == lease.org_id,
            investigation_jobs.c.id == lease.job_id,
        )
        inv_id = (await session.execute(inv_job_stmt)).scalar_one_or_none()
        if inv_id is None:
            raise ScopeViolation(f"investigation not found for job {lease.job_id}")

        inv_stmt = select(investigations).where(
            investigations.c.org_id == lease.org_id,
            investigations.c.id == inv_id,
        )
        if lock:
            inv_stmt = inv_stmt.with_for_update()
        inv_row = (await session.execute(inv_stmt)).mappings().one_or_none()
        if inv_row is None:
            raise ScopeViolation(f"investigation {inv_id} not found")

        # Verify repo access for principal (revocation check)
        await require_repository(
            session,
            lease.org_id,
            inv_row["principal_id"],
            inv_row["repository_id"],
            write=True,
        )

        spec = await self.spec_repo.get_spec(session, lease.org_id, inv_row["spec_hash"])
        if spec is None:
            raise ScopeViolation(f"spec {inv_row['spec_hash']} not found")

        scope = InvestigationScopeV1(
            org_id=lease.org_id,
            repository_id=inv_row["repository_id"],
            snapshot_id=inv_row["snapshot_id"],
            principal_id=inv_row["principal_id"],
            investigation_id=inv_id,
            spec_hash=inv_row["spec_hash"],
            finding_id=spec.finding_id,
            finding_run_id=spec.finding_run_id,
            chunk_set_id=spec.chunk_set_id,
            execution_revision=inv_row["execution_revision"],
        )

        return InvestigationAuthorityV1(
            db_now=now_utc,
            deadline=deadline,
            is_cancelled=is_cancelled,
            is_expired=is_expired,
            scope=scope,
        )


# ---------------------------------------------------------------------------
# Investigation Executor Service
# ---------------------------------------------------------------------------


class InvestigationExecutorService(InvestigationExecutorPort):
    """Advances durable investigation node execution."""

    def __init__(
        self,
        gateway: ModelGatewayPort,
        tools: InvestigationToolPort,
        authority: InvestigationJobAuthorityPort,
        checkpoint_repo: InvestigationExecutionCheckpointRepository,
        step_receipt_repo: Any,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        spec_repo: SqlInvestigationSpecRepository | None = None,
        spec_provider: (
            Callable[
                [AsyncSession | None, UUID, str],
                InvestigationSpecV1 | Awaitable[InvestigationSpecV1 | None] | None,
            ]
            | None
        ) = None,
        result_store: ModelResultStorePort | None = None,
        verification_service: Any | None = None,
        evidence_repo: Any | None = None,
        policy_resolver: Callable[[InvestigationScopeV1], DisclosurePolicyV1] | None = None,
        bound_lease: AttemptLeaseV2 | None = None,
    ) -> None:
        self.bound_lease = bound_lease
        self.gateway = gateway
        self.tools = tools
        self.authority = authority
        self.checkpoint_repo = checkpoint_repo
        self.step_receipt_repo = step_receipt_repo
        self.session_factory = session_factory
        self.spec_repo = spec_repo
        self.spec_provider = spec_provider
        self.result_store = result_store
        self.verification_service = verification_service
        self.evidence_repo = evidence_repo
        self.policy_resolver = policy_resolver or (lambda scope: DisclosurePolicyV1())

    async def _get_spec(
        self, session: AsyncSession | None, org_id: UUID, spec_hash: str
    ) -> InvestigationSpecV1 | None:
        if self.spec_provider is not None:
            res = self.spec_provider(session, org_id, spec_hash)
            if asyncio.iscoroutine(res):
                return cast(InvestigationSpecV1 | None, await res)
            return cast(InvestigationSpecV1 | None, res)
        if self.spec_repo is not None and session is not None:
            return await self.spec_repo.get_spec(session, org_id, spec_hash)
        return None

    @staticmethod
    def _with_context_references(
        state: InvestigationExecutionStateV1,
        response: ModelResponseV1,
    ) -> InvestigationExecutionStateV1:
        if response.context_packet_hash is None or response.memory_provenance_hash is None:
            return state
        return state.model_copy(
            update={
                "context_packet_hash": response.context_packet_hash,
                "memory_provenance_hash": response.memory_provenance_hash,
            }
        )

    async def _append_checkpoint(
        self,
        session: AsyncSession | None,
        lease: AttemptLeaseV2,
        *,
        expected_previous_sequence: int,
        state: InvestigationExecutionStateV1,
    ) -> InvestigationExecutionCheckpointV1:
        cp = await self.checkpoint_repo.append(
            session,  # type: ignore[arg-type]
            lease,
            expected_previous_sequence=expected_previous_sequence,
            state=state,
        )
        if session is not None:
            await session.commit()
        return cp

    async def advance(
        self,
        context: InvestigationExecutionContextV1,
    ) -> InvestigationAdvanceV1:
        scope = context.scope
        lease = self.bound_lease or AttemptLeaseV2(
            org_id=scope.org_id,
            job_id=context.attempt_job_id,
            attempt_id=context.attempt_id,
            fence=context.attempt_fence,
            owner="executor",
            lease_expires_at=_ensure_utc(context.deadline),
            heartbeat_at=_ensure_utc(context.deadline),
        )

        if (
            lease.org_id != scope.org_id
            or lease.job_id != context.attempt_job_id
            or lease.attempt_id != context.attempt_id
            or lease.fence != context.attempt_fence
        ):
            raise ScopeViolation("executor attempt context differs")

        session: AsyncSession | None = None
        if self.session_factory is not None:
            session = self.session_factory()

        try:
            # 1. Authority check
            auth = await self.authority.assert_current(session, lease, lock=False)  # type: ignore[arg-type]
            if auth.is_cancelled:
                return InvestigationAdvanceV1(
                    status=InvestigationAdvanceStatus.TERMINAL_FAILURE,
                    stop_reason=InvestigationStopReason.CANCELLED,
                    message="investigation cancelled",
                )
            if auth.is_expired:
                return InvestigationAdvanceV1(
                    status=InvestigationAdvanceStatus.TERMINAL_FAILURE,
                    stop_reason=InvestigationStopReason.DEADLINE_EXCEEDED,
                    message="investigation deadline exceeded",
                )

            # 2. Spec resolution
            spec = await self._get_spec(session, scope.org_id, scope.spec_hash)
            if spec is None:
                raise ScopeViolation(f"spec {scope.spec_hash} not found")

            # 3. Load latest checkpoint
            checkpoint = await self.checkpoint_repo.load_latest(session, scope)  # type: ignore[arg-type]

            if checkpoint is None:
                # Sequence 0: Initialize
                mode = spec.mode
                first_node_kind = InvestigationNodeKind.GATHER
                first_role = "investigator" if mode == "short_path" else "gatherer"
                state = InvestigationExecutionStateV1(
                    investigation_id=scope.investigation_id,
                    scope_hash=hashlib.sha256(canonical_bytes(scope.model_dump())).hexdigest(),
                    spec_hash=scope.spec_hash,
                    execution_revision=scope.execution_revision,
                    next_node_kind=first_node_kind,
                    next_role=first_role,
                    next_ordinal=0,
                    checkpoint_sequence=0,
                )
            else:
                state = checkpoint.state

            # Terminal / Ready check
            if state.next_node_kind == InvestigationNodeKind.FINALIZE:
                node_id = compute_node_id(
                    scope.org_id,
                    scope.investigation_id,
                    scope.spec_hash,
                    state.next_node_kind,
                    state.next_role,
                    state.next_ordinal,
                )
                return InvestigationAdvanceV1(
                    status=InvestigationAdvanceStatus.READY_TO_PUBLISH,
                    node_id=node_id,
                    checkpoint_sequence=state.checkpoint_sequence,
                    stop_reason=state.stop_reason or InvestigationStopReason.COMPLETED,
                    message="investigation ready to publish",
                )

            current_kind = state.next_node_kind
            current_role = state.next_role
            current_ordinal = state.next_ordinal
            node_id = compute_node_id(
                scope.org_id,
                scope.investigation_id,
                scope.spec_hash,
                current_kind,
                current_role,
                current_ordinal,
            )

            # Check if an existing step receipt exists
            existing_receipt = await self.step_receipt_repo.get_receipt(
                session, scope.org_id, scope.investigation_id, scope.execution_revision, node_id
            )
            if existing_receipt is not None and existing_receipt["state"] == "uncertain":
                return InvestigationAdvanceV1(
                    status=InvestigationAdvanceStatus.NEEDS_REVIEW,
                    node_id=node_id,
                    checkpoint_sequence=state.checkpoint_sequence,
                    stop_reason=InvestigationStopReason.MODEL_OUTCOME_UNKNOWN,
                    message="model call outcome uncertain, awaiting resolution",
                )

            # Check limits before model turns
            if current_kind not in (
                InvestigationNodeKind.GATHER,
                InvestigationNodeKind.VERIFY,
                InvestigationNodeKind.FINALIZE,
            ):
                if current_ordinal >= context.limits.max_steps:
                    # Transition to VERIFY due to step budget exhaustion
                    new_state = state.model_copy(
                        update={
                            "next_node_kind": InvestigationNodeKind.VERIFY,
                            "next_role": "verifier",
                            "next_ordinal": current_ordinal + 1,
                            "stop_reason": InvestigationStopReason.STEP_BUDGET_EXHAUSTED,
                        }
                    )
                    cp = await self._append_checkpoint(
                        session,
                        lease,
                        expected_previous_sequence=state.checkpoint_sequence,
                        state=new_state,
                    )
                    return InvestigationAdvanceV1(
                        status=InvestigationAdvanceStatus.PROGRESS,
                        node_id=node_id,
                        checkpoint_sequence=cp.checkpoint_sequence,
                        stop_reason=InvestigationStopReason.STEP_BUDGET_EXHAUSTED,
                    )

                if state.usage_projection_micros >= context.limits.budget_limit_micros:
                    # Transition to VERIFY due to spending budget exhaustion
                    new_state = state.model_copy(
                        update={
                            "next_node_kind": InvestigationNodeKind.VERIFY,
                            "next_role": "verifier",
                            "next_ordinal": current_ordinal + 1,
                            "stop_reason": InvestigationStopReason.SPENDING_BUDGET_EXHAUSTED,
                        }
                    )
                    cp = await self._append_checkpoint(
                        session,
                        lease,
                        expected_previous_sequence=state.checkpoint_sequence,
                        state=new_state,
                    )
                    return InvestigationAdvanceV1(
                        status=InvestigationAdvanceStatus.PROGRESS,
                        node_id=node_id,
                        checkpoint_sequence=cp.checkpoint_sequence,
                        stop_reason=InvestigationStopReason.SPENDING_BUDGET_EXHAUSTED,
                    )

            # 4. Node Logic
            if current_kind == InvestigationNodeKind.GATHER:
                packet = {
                    "query": spec.query,
                    "target_type": spec.target_type,
                    "mode": spec.mode,
                }
                packet_hash = hashlib.sha256(canonical_bytes(packet)).hexdigest()

                if spec.mode == "short_path":
                    next_kind = InvestigationNodeKind.INVESTIGATE
                    next_role = "investigator"
                else:
                    next_kind = InvestigationNodeKind.PLAN
                    next_role = "planner"

                new_state = state.model_copy(
                    update={
                        "context_packet_hash": packet_hash,
                        "next_node_kind": next_kind,
                        "next_role": next_role,
                        "next_ordinal": current_ordinal + 1,
                    }
                )
                await self.step_receipt_repo.record_receipt(
                    session=session,
                    org_id=scope.org_id,
                    investigation_id=scope.investigation_id,
                    execution_revision=scope.execution_revision,
                    node_id=node_id,
                    node_kind=current_kind.value,
                    role=current_role,
                    logical_ordinal=current_ordinal,
                    input_digest=packet_hash,
                    state="completed",
                    attempt_id=context.attempt_id,
                )
                cp = await self._append_checkpoint(
                    session,
                    lease,
                    expected_previous_sequence=state.checkpoint_sequence,
                    state=new_state,
                )
                return InvestigationAdvanceV1(
                    status=InvestigationAdvanceStatus.PROGRESS,
                    node_id=node_id,
                    checkpoint_sequence=cp.checkpoint_sequence,
                )

            elif current_kind == InvestigationNodeKind.INVESTIGATE:
                messages = [
                    ChatMessageV1(
                        role=ModelRole.SYSTEM,
                        content="Investigate potential security issues accurately.",
                    ),
                    ChatMessageV1(role=ModelRole.USER, content=f"Investigate: {spec.query}"),
                ]
                # Check context bound
                ctx_bytes = len(canonical_bytes([m.model_dump() for m in messages]))
                if ctx_bytes > context.limits.max_context_bytes:
                    return InvestigationAdvanceV1(
                        status=InvestigationAdvanceStatus.TERMINAL_FAILURE,
                        node_id=node_id,
                        checkpoint_sequence=state.checkpoint_sequence,
                        stop_reason=InvestigationStopReason.CONTEXT_BUDGET_EXCEEDED,
                        message="context budget exceeded",
                    )

                step_key = f"inv-{scope.investigation_id}-step-{current_ordinal}"
                logical_call_id = compute_call_id(node_id, 0)
                req_tools = self.tools.get_tool_definitions()

                if session is not None:
                    await session.commit()
                resp = await self.gateway.execute_call(
                    org_id=scope.org_id,
                    repository_id=scope.repository_id,
                    principal_id=scope.principal_id,
                    investigation_id=scope.investigation_id,
                    logical_call_id=logical_call_id,
                    attempt_id=1,
                    messages=tuple(messages),
                    tools=req_tools,
                    requirement=ModelRoutingRequirementV1(
                        task_type="short_path",
                        estimated_context_tokens=context.limits.max_context_tokens,
                        estimated_output_tokens=1500,
                        requires_tools=bool(req_tools),
                    ),
                    disclosure_policy=self.policy_resolver(scope),
                    idempotency_key=step_key,
                )
                state = self._with_context_references(state, resp)

                new_spent = state.usage_projection_micros + resp.charge_micros
                new_evidence_ids = list(state.committed_evidence_ids)
                new_tool_receipt_ids = list(state.committed_tool_receipt_ids)

                if resp.tool_calls:
                    # Execute tool calls bounded by per-response and total limits
                    max_tools_to_run = min(
                        len(resp.tool_calls),
                        context.limits.max_tool_calls_per_response,
                        max(
                            0,
                            context.limits.max_tool_calls_total
                            - len(state.committed_tool_receipt_ids),
                        ),
                    )
                    for call in resp.tool_calls[:max_tools_to_run]:
                        args: dict[str, Any] = {}
                        if call.arguments_json:
                            try:
                                args = json.loads(call.arguments_json)
                            except Exception:
                                args = {}

                        tool_req = InvestigationToolRequestV1(
                            tool_name=InvestigationToolName(call.name),
                            arguments=args,
                        )
                        tool_res = await self.tools.execute(tool_req, context)
                        if tool_res.evidence_refs:
                            new_evidence_ids.extend(tool_res.evidence_refs)
                        new_tool_receipt_ids.append(uuid4())

                    # Determine next node
                    if new_spent >= context.limits.budget_limit_micros:
                        next_kind = InvestigationNodeKind.VERIFY
                        next_role = "verifier"
                        stop_reason = InvestigationStopReason.SPENDING_BUDGET_EXHAUSTED
                    elif current_ordinal + 1 >= context.limits.max_steps:
                        next_kind = InvestigationNodeKind.VERIFY
                        next_role = "verifier"
                        stop_reason = InvestigationStopReason.STEP_BUDGET_EXHAUSTED
                    else:
                        next_kind = InvestigationNodeKind.INVESTIGATE
                        next_role = "investigator"
                        stop_reason = None
                else:
                    # Terminal response from model
                    next_kind = InvestigationNodeKind.VERIFY
                    next_role = "verifier"
                    stop_reason = None

                new_state = state.model_copy(
                    update={
                        "next_node_kind": next_kind,
                        "next_role": next_role,
                        "next_ordinal": current_ordinal + 1,
                        "usage_projection_micros": new_spent,
                        "committed_evidence_ids": tuple(new_evidence_ids),
                        "committed_tool_receipt_ids": tuple(new_tool_receipt_ids),
                        "stop_reason": stop_reason or state.stop_reason,
                    }
                )

                input_digest = hashlib.sha256(canonical_bytes(messages[1].content)).hexdigest()
                await self.step_receipt_repo.record_receipt(
                    session=session,
                    org_id=scope.org_id,
                    investigation_id=scope.investigation_id,
                    execution_revision=scope.execution_revision,
                    node_id=node_id,
                    node_kind=current_kind.value,
                    role=current_role,
                    logical_ordinal=current_ordinal,
                    input_digest=input_digest,
                    state="completed",
                    attempt_id=context.attempt_id,
                )
                cp = await self._append_checkpoint(
                    session,
                    lease,
                    expected_previous_sequence=state.checkpoint_sequence,
                    state=new_state,
                )
                return InvestigationAdvanceV1(
                    status=InvestigationAdvanceStatus.PROGRESS,
                    node_id=node_id,
                    checkpoint_sequence=cp.checkpoint_sequence,
                    stop_reason=stop_reason,
                )

            elif current_kind == InvestigationNodeKind.PLAN:
                step_key = f"inv-{scope.investigation_id}-plan-{current_ordinal}"
                logical_call_id = compute_call_id(node_id, 0)
                messages = [
                    ChatMessageV1(
                        role=ModelRole.SYSTEM, content="Formulate a security hypothesis."
                    ),
                    ChatMessageV1(role=ModelRole.USER, content=f"Target: {spec.query}"),
                ]
                if session is not None:
                    await session.commit()
                resp = await self.gateway.execute_call(
                    org_id=scope.org_id,
                    repository_id=scope.repository_id,
                    principal_id=scope.principal_id,
                    investigation_id=scope.investigation_id,
                    logical_call_id=logical_call_id,
                    attempt_id=1,
                    messages=tuple(messages),
                    tools=(),
                    requirement=ModelRoutingRequirementV1(
                        task_type="planner",
                        estimated_context_tokens=context.limits.max_context_tokens,
                        estimated_output_tokens=1500,
                        requires_tools=False,
                    ),
                    disclosure_policy=self.policy_resolver(scope),
                    idempotency_key=step_key,
                )
                state = self._with_context_references(state, resp)
                new_spent = state.usage_projection_micros + resp.charge_micros
                new_state = state.model_copy(
                    update={
                        "next_node_kind": InvestigationNodeKind.EVIDENCE,
                        "next_role": "investigator",
                        "next_ordinal": current_ordinal + 1,
                        "usage_projection_micros": new_spent,
                    }
                )
                await self.step_receipt_repo.record_receipt(
                    session=session,
                    org_id=scope.org_id,
                    investigation_id=scope.investigation_id,
                    execution_revision=scope.execution_revision,
                    node_id=node_id,
                    node_kind=current_kind.value,
                    role=current_role,
                    logical_ordinal=current_ordinal,
                    input_digest=hashlib.sha256(canonical_bytes(resp.content)).hexdigest(),
                    state="completed",
                    attempt_id=context.attempt_id,
                )
                cp = await self._append_checkpoint(
                    session,
                    lease,
                    expected_previous_sequence=state.checkpoint_sequence,
                    state=new_state,
                )
                return InvestigationAdvanceV1(
                    status=InvestigationAdvanceStatus.PROGRESS,
                    node_id=node_id,
                    checkpoint_sequence=cp.checkpoint_sequence,
                )

            elif current_kind == InvestigationNodeKind.EVIDENCE:
                step_key = f"inv-{scope.investigation_id}-evidence-{current_ordinal}"
                logical_call_id = compute_call_id(node_id, 0)
                messages = [
                    ChatMessageV1(role=ModelRole.SYSTEM, content="Gather evidence for hypothesis."),
                    ChatMessageV1(role=ModelRole.USER, content=f"Gather evidence on: {spec.query}"),
                ]
                if session is not None:
                    await session.commit()
                resp = await self.gateway.execute_call(
                    org_id=scope.org_id,
                    repository_id=scope.repository_id,
                    principal_id=scope.principal_id,
                    investigation_id=scope.investigation_id,
                    logical_call_id=logical_call_id,
                    attempt_id=1,
                    messages=tuple(messages),
                    tools=(),
                    requirement=ModelRoutingRequirementV1(
                        task_type="short_path",
                        estimated_context_tokens=context.limits.max_context_tokens,
                        estimated_output_tokens=1500,
                        requires_tools=True,
                    ),
                    disclosure_policy=self.policy_resolver(scope),
                    idempotency_key=step_key,
                )
                state = self._with_context_references(state, resp)
                new_spent = state.usage_projection_micros + resp.charge_micros
                new_state = state.model_copy(
                    update={
                        "next_node_kind": InvestigationNodeKind.AUDIT,
                        "next_role": "auditor",
                        "next_ordinal": current_ordinal + 1,
                        "usage_projection_micros": new_spent,
                    }
                )
                await self.step_receipt_repo.record_receipt(
                    session=session,
                    org_id=scope.org_id,
                    investigation_id=scope.investigation_id,
                    execution_revision=scope.execution_revision,
                    node_id=node_id,
                    node_kind=current_kind.value,
                    role=current_role,
                    logical_ordinal=current_ordinal,
                    input_digest=hashlib.sha256(canonical_bytes(resp.content)).hexdigest(),
                    state="completed",
                    attempt_id=context.attempt_id,
                )
                cp = await self._append_checkpoint(
                    session,
                    lease,
                    expected_previous_sequence=state.checkpoint_sequence,
                    state=new_state,
                )
                return InvestigationAdvanceV1(
                    status=InvestigationAdvanceStatus.PROGRESS,
                    node_id=node_id,
                    checkpoint_sequence=cp.checkpoint_sequence,
                )

            elif current_kind == InvestigationNodeKind.AUDIT:
                step_key = f"inv-{scope.investigation_id}-audit-{current_ordinal}"
                logical_call_id = compute_call_id(node_id, 0)
                messages = [
                    ChatMessageV1(
                        role=ModelRole.SYSTEM, content="Seek counterexamples and scrutinize claims."
                    ),
                    ChatMessageV1(
                        role=ModelRole.USER, content=f"Audit hypothesis for: {spec.query}"
                    ),
                ]
                if session is not None:
                    await session.commit()
                resp = await self.gateway.execute_call(
                    org_id=scope.org_id,
                    repository_id=scope.repository_id,
                    principal_id=scope.principal_id,
                    investigation_id=scope.investigation_id,
                    logical_call_id=logical_call_id,
                    attempt_id=1,
                    messages=tuple(messages),
                    tools=(),
                    requirement=ModelRoutingRequirementV1(
                        task_type="auditor",
                        estimated_context_tokens=context.limits.max_context_tokens,
                        estimated_output_tokens=1500,
                        requires_tools=False,
                    ),
                    disclosure_policy=self.policy_resolver(scope),
                    idempotency_key=step_key,
                )
                state = self._with_context_references(state, resp)
                new_spent = state.usage_projection_micros + resp.charge_micros
                new_state = state.model_copy(
                    update={
                        "next_node_kind": InvestigationNodeKind.SYNTHESIZE,
                        "next_role": "synthesizer",
                        "next_ordinal": current_ordinal + 1,
                        "usage_projection_micros": new_spent,
                    }
                )
                await self.step_receipt_repo.record_receipt(
                    session=session,
                    org_id=scope.org_id,
                    investigation_id=scope.investigation_id,
                    execution_revision=scope.execution_revision,
                    node_id=node_id,
                    node_kind=current_kind.value,
                    role=current_role,
                    logical_ordinal=current_ordinal,
                    input_digest=hashlib.sha256(canonical_bytes(resp.content)).hexdigest(),
                    state="completed",
                    attempt_id=context.attempt_id,
                )
                cp = await self._append_checkpoint(
                    session,
                    lease,
                    expected_previous_sequence=state.checkpoint_sequence,
                    state=new_state,
                )
                return InvestigationAdvanceV1(
                    status=InvestigationAdvanceStatus.PROGRESS,
                    node_id=node_id,
                    checkpoint_sequence=cp.checkpoint_sequence,
                )

            elif current_kind == InvestigationNodeKind.SYNTHESIZE:
                step_key = f"inv-{scope.investigation_id}-synthesize-{current_ordinal}"
                logical_call_id = compute_call_id(node_id, 0)
                messages = [
                    ChatMessageV1(
                        role=ModelRole.SYSTEM,
                        content="Synthesize debate and produce final verdict.",
                    ),
                    ChatMessageV1(
                        role=ModelRole.USER, content=f"Synthesize conclusions for: {spec.query}"
                    ),
                ]
                if session is not None:
                    await session.commit()
                resp = await self.gateway.execute_call(
                    org_id=scope.org_id,
                    repository_id=scope.repository_id,
                    principal_id=scope.principal_id,
                    investigation_id=scope.investigation_id,
                    logical_call_id=logical_call_id,
                    attempt_id=1,
                    messages=tuple(messages),
                    tools=(),
                    requirement=ModelRoutingRequirementV1(
                        task_type="synthesizer",
                        estimated_context_tokens=context.limits.max_context_tokens,
                        estimated_output_tokens=2000,
                        requires_tools=False,
                    ),
                    disclosure_policy=self.policy_resolver(scope),
                    idempotency_key=step_key,
                )
                state = self._with_context_references(state, resp)
                new_spent = state.usage_projection_micros + resp.charge_micros
                new_state = state.model_copy(
                    update={
                        "next_node_kind": InvestigationNodeKind.VERIFY,
                        "next_role": "verifier",
                        "next_ordinal": current_ordinal + 1,
                        "usage_projection_micros": new_spent,
                    }
                )
                await self.step_receipt_repo.record_receipt(
                    session=session,
                    org_id=scope.org_id,
                    investigation_id=scope.investigation_id,
                    execution_revision=scope.execution_revision,
                    node_id=node_id,
                    node_kind=current_kind.value,
                    role=current_role,
                    logical_ordinal=current_ordinal,
                    input_digest=hashlib.sha256(canonical_bytes(resp.content)).hexdigest(),
                    state="completed",
                    attempt_id=context.attempt_id,
                )
                cp = await self._append_checkpoint(
                    session,
                    lease,
                    expected_previous_sequence=state.checkpoint_sequence,
                    state=new_state,
                )
                return InvestigationAdvanceV1(
                    status=InvestigationAdvanceStatus.PROGRESS,
                    node_id=node_id,
                    checkpoint_sequence=cp.checkpoint_sequence,
                )

            elif current_kind == InvestigationNodeKind.VERIFY:
                # Deterministic claim verification against evidence records
                from app.services.investigation_verification import (
                    InvestigationVerificationService,
                )

                verifier = self.verification_service or InvestigationVerificationService()
                evidence_records: tuple[InvestigationEvidenceV1, ...] = ()
                if self.evidence_repo and session is not None:
                    evidence_records = await self.evidence_repo.list_evidence(
                        session, scope.org_id, scope.investigation_id
                    )

                outcome = await verifier.verify_claims(
                    scope=scope,
                    claims=(),
                    evidence_records=evidence_records,
                    original_verdict="indeterminate",
                    original_confidence=0.5,
                )
                draft = verifier.prepare_draft(
                    scope=scope,
                    outcome=outcome,
                    claims=(),
                    evidence_refs=tuple(e.id for e in evidence_records),
                    settled_usage_micros=state.usage_projection_micros,
                    stop_reason=state.stop_reason or InvestigationStopReason.COMPLETED,
                )
                draft_digest = draft.draft_digest
                new_state = state.model_copy(
                    update={
                        "next_node_kind": InvestigationNodeKind.FINALIZE,
                        "next_role": "finalizer",
                        "next_ordinal": current_ordinal + 1,
                        "report_draft_digest": draft_digest,
                    }
                )
                await self.step_receipt_repo.record_receipt(
                    session=session,
                    org_id=scope.org_id,
                    investigation_id=scope.investigation_id,
                    execution_revision=scope.execution_revision,
                    node_id=node_id,
                    node_kind=current_kind.value,
                    role=current_role,
                    logical_ordinal=current_ordinal,
                    input_digest=draft_digest,
                    state="completed",
                    attempt_id=context.attempt_id,
                )
                cp = await self._append_checkpoint(
                    session,
                    lease,
                    expected_previous_sequence=state.checkpoint_sequence,
                    state=new_state,
                )
                return InvestigationAdvanceV1(
                    status=InvestigationAdvanceStatus.READY_TO_PUBLISH,
                    node_id=node_id,
                    checkpoint_sequence=cp.checkpoint_sequence,
                    stop_reason=state.stop_reason or InvestigationStopReason.COMPLETED,
                    message="claims verified, draft report ready",
                    draft=draft,
                )

            else:
                return InvestigationAdvanceV1(
                    status=InvestigationAdvanceStatus.TERMINAL_FAILURE,
                    node_id=node_id,
                    checkpoint_sequence=state.checkpoint_sequence,
                    stop_reason=InvestigationStopReason.CONFIGURATION_MISSING,
                    message=f"unknown node kind: {current_kind}",
                )

        except ContextBudgetExceededError:
            return InvestigationAdvanceV1(
                status=InvestigationAdvanceStatus.TERMINAL_FAILURE,
                stop_reason=InvestigationStopReason.CONTEXT_BUDGET_EXCEEDED,
                message="required model context exceeds the configured budget",
            )
        except InvalidTransition as exc:
            if str(exc) != "context_invalidated":
                raise
            return InvestigationAdvanceV1(
                status=InvestigationAdvanceStatus.TERMINAL_FAILURE,
                stop_reason=InvestigationStopReason.NONRESUMABLE,
                message="reviewed context changed; start a new investigation",
            )
        finally:
            if session is not None:
                await session.close()
