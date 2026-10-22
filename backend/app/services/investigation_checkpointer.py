"""Durable fenced checkpointer and lifecycle service for agent investigations."""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import desc, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.investigation import (
    InvestigationCreateRequestV1,
    InvestigationEventType,
    InvestigationEventV1,
    InvestigationStateV1,
    InvestigationStatus,
    InvestigationSummaryV1,
)
from app.models.investigation_tables import (
    investigation_checkpoints,
    investigation_events,
    investigations,
)

logger = logging.getLogger(__name__)


class InvestigationCheckpointerError(Exception):
    """Base exception for investigation checkpointer errors."""


class InvestigationNotFoundError(InvestigationCheckpointerError):
    """Investigation does not exist for the given tenant."""


class LeaseAcquisitionError(InvestigationCheckpointerError):
    """Worker lease is held by another active worker."""


class StaleWorkerFencingError(InvestigationCheckpointerError):
    """Worker fencing token or lease has expired / been superseded."""


class InvestigationTerminatedError(InvestigationCheckpointerError):
    """Investigation has already concluded or been cancelled."""


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


class FencedCheckpointer:
    """Provides atomic worker lease management, strictly monotonic fencing,
    durable checkpoint snapshots, and append-only event auditing.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def create_investigation(
        self,
        org_id: UUID,
        principal_id: UUID,
        req: InvestigationCreateRequestV1,
    ) -> InvestigationSummaryV1:
        inv_id = uuid4()
        now = datetime.now(UTC)
        stmt = investigations.insert().values(
            org_id=org_id,
            id=inv_id,
            repository_id=req.repository_id,
            principal_id=principal_id,
            finding_id=req.finding_id,
            target_type=req.target_type.value,
            query=req.query,
            status=InvestigationStatus.PENDING.value,
            mode=req.mode.value,
            depth=req.depth.value,
            current_step=0,
            max_steps=req.max_steps,
            fencing_token=1,
            worker_id=None,
            worker_lease_expires_at=None,
            budget_limit_micros=req.budget_limit_micros,
            spent_micros=0,
            verdict=None,
            verdict_confidence=None,
            summary=None,
            failure_reason=None,
            created_at=now,
            updated_at=now,
        )
        await self.s.execute(stmt)

        return InvestigationSummaryV1(
            id=inv_id,
            org_id=org_id,
            repository_id=req.repository_id,
            finding_id=req.finding_id,
            target_type=req.target_type.value,
            query=req.query,
            status=InvestigationStatus.PENDING.value,
            mode=req.mode.value,
            depth=req.depth.value,
            current_step=0,
            max_steps=req.max_steps,
            fencing_token=1,
            worker_id=None,
            budget_limit_micros=req.budget_limit_micros,
            spent_micros=0,
            verdict=None,
            verdict_confidence=None,
            summary=None,
            failure_reason=None,
            created_at=now,
            updated_at=now,
        )

    async def get_investigation(self, org_id: UUID, investigation_id: UUID) -> dict[str, Any]:
        stmt = select(investigations).where(
            investigations.c.org_id == org_id,
            investigations.c.id == investigation_id,
        )
        row = (await self.s.execute(stmt)).mappings().one_or_none()
        if not row:
            raise InvestigationNotFoundError(f"Investigation {investigation_id} not found")
        return dict(row)

    async def acquire_lease(
        self,
        org_id: UUID,
        investigation_id: UUID,
        worker_id: str,
        lease_ttl_seconds: int = 30,
    ) -> int:
        """Atomically acquires worker execution lease and increments the fencing token."""
        stmt = (
            select(investigations)
            .where(
                investigations.c.org_id == org_id,
                investigations.c.id == investigation_id,
            )
            .with_for_update()
        )
        inv = (await self.s.execute(stmt)).mappings().one_or_none()
        if not inv:
            raise InvestigationNotFoundError(f"Investigation {investigation_id} not found")

        if inv["status"] in (
            InvestigationStatus.COMPLETED.value,
            InvestigationStatus.FAILED.value,
            InvestigationStatus.CANCELLED.value,
        ):
            raise InvestigationTerminatedError(
                f"Investigation {investigation_id} is in terminal status '{inv['status']}'"
            )

        now = datetime.now(UTC)
        lease_exp = _as_utc(inv["worker_lease_expires_at"])
        is_lease_active = lease_exp is not None and lease_exp > now
        if is_lease_active and inv["worker_id"] != worker_id:
            exp = inv["worker_lease_expires_at"]
            raise LeaseAcquisitionError(
                f"Active lease held by worker '{inv['worker_id']}' until {exp}"
            )

        new_fencing_token = int(inv["fencing_token"]) + 1
        lease_expires = now + timedelta(seconds=lease_ttl_seconds)

        await self.s.execute(
            update(investigations)
            .where(
                investigations.c.org_id == org_id,
                investigations.c.id == investigation_id,
            )
            .values(
                worker_id=worker_id,
                worker_lease_expires_at=lease_expires,
                fencing_token=new_fencing_token,
                status=InvestigationStatus.RUNNING.value,
                updated_at=now,
            )
        )
        return new_fencing_token

    async def renew_lease(
        self,
        org_id: UUID,
        investigation_id: UUID,
        worker_id: str,
        fencing_token: int,
        lease_ttl_seconds: int = 30,
    ) -> None:
        """Extends worker lease if fencing token and worker ID match."""
        now = datetime.now(UTC)
        lease_expires = now + timedelta(seconds=lease_ttl_seconds)

        stmt = (
            update(investigations)
            .where(
                investigations.c.org_id == org_id,
                investigations.c.id == investigation_id,
                investigations.c.worker_id == worker_id,
                investigations.c.fencing_token == fencing_token,
            )
            .values(
                worker_lease_expires_at=lease_expires,
                updated_at=now,
            )
        )
        res = await self.s.execute(stmt)
        rowcount = getattr(res, "rowcount", -1)
        if rowcount == 0:
            raise StaleWorkerFencingError(
                f"Cannot renew lease: worker '{worker_id}' with fence {fencing_token} "
                "has been superseded"
            )

    async def save_checkpoint(
        self,
        org_id: UUID,
        investigation_id: UUID,
        worker_id: str,
        fencing_token: int,
        step_index: int,
        graph_node: str,
        state: InvestigationStateV1,
        new_spent_micros: int = 0,
    ) -> None:
        """Atomically saves a step checkpoint snapshot guarded by worker fencing verification."""
        stmt = (
            select(investigations)
            .where(
                investigations.c.org_id == org_id,
                investigations.c.id == investigation_id,
            )
            .with_for_update()
        )
        inv = (await self.s.execute(stmt)).mappings().one_or_none()
        if not inv:
            raise InvestigationNotFoundError(f"Investigation {investigation_id} not found")

        now = datetime.now(UTC)
        # Verify fencing token
        if inv["fencing_token"] != fencing_token or inv["worker_id"] != worker_id:
            raise StaleWorkerFencingError(
                f"Worker fence superseded: worker={worker_id}, fence={fencing_token}, "
                f"current_worker={inv['worker_id']}, current_fence={inv['fencing_token']}"
            )

        # Verify lease has not expired
        lease_exp = _as_utc(inv["worker_lease_expires_at"])
        if lease_exp is None or lease_exp <= now:
            raise StaleWorkerFencingError(
                f"Worker lease expired at {inv['worker_lease_expires_at']}"
            )

        # Insert or replace checkpoint snapshot
        stmt_cp = investigation_checkpoints.insert().values(
            org_id=org_id,
            investigation_id=investigation_id,
            step_index=step_index,
            fencing_token=fencing_token,
            worker_id=worker_id,
            graph_node=graph_node,
            state_snapshot_json=state.model_dump(mode="json"),
            created_at=now,
        )
        await self.s.execute(stmt_cp)

        # Update investigation master state
        new_total_spent = inv["spent_micros"] + new_spent_micros
        await self.s.execute(
            update(investigations)
            .where(
                investigations.c.org_id == org_id,
                investigations.c.id == investigation_id,
            )
            .values(
                current_step=step_index,
                status=InvestigationStatus.CHECKPOINTED.value,
                spent_micros=new_total_spent,
                verdict=state.verdict,
                verdict_confidence=state.verdict_confidence,
                summary=state.summary,
                updated_at=now,
            )
        )

        # Append audit event
        await self.append_event(
            org_id=org_id,
            investigation_id=investigation_id,
            step_index=step_index,
            event_type=InvestigationEventType.CHECKPOINT_SAVED.value,
            node_name=graph_node,
            payload={
                "fencing_token": fencing_token,
                "worker_id": worker_id,
                "step_index": step_index,
                "spent_micros": new_total_spent,
            },
        )

    async def load_latest_checkpoint(
        self,
        org_id: UUID,
        investigation_id: UUID,
    ) -> tuple[int, InvestigationStateV1] | None:
        """Retrieves the latest durable checkpoint for resuming an execution graph."""
        stmt = (
            select(investigation_checkpoints)
            .where(
                investigation_checkpoints.c.org_id == org_id,
                investigation_checkpoints.c.investigation_id == investigation_id,
            )
            .order_by(desc(investigation_checkpoints.c.step_index))
            .limit(1)
        )
        row = (await self.s.execute(stmt)).mappings().one_or_none()
        if not row:
            return None

        state = InvestigationStateV1.model_validate(row["state_snapshot_json"])
        return row["step_index"], state

    async def append_event(
        self,
        org_id: UUID,
        investigation_id: UUID,
        step_index: int,
        event_type: str,
        node_name: str,
        payload: dict[str, Any],
    ) -> UUID:
        """Appends an event to the investigation's chronological replay stream."""
        event_id = uuid4()
        now = datetime.now(UTC)
        stmt = investigation_events.insert().values(
            org_id=org_id,
            id=event_id,
            investigation_id=investigation_id,
            step_index=step_index,
            event_type=event_type,
            node_name=node_name,
            payload_json=payload,
            created_at=now,
        )
        await self.s.execute(stmt)
        return event_id

    async def list_events(
        self,
        org_id: UUID,
        investigation_id: UUID,
        after_step: int | None = None,
    ) -> list[InvestigationEventV1]:
        """Lists events chronologically for real-time frontend streaming and replay."""
        stmt = select(investigation_events).where(
            investigation_events.c.org_id == org_id,
            investigation_events.c.investigation_id == investigation_id,
        )
        if after_step is not None:
            stmt = stmt.where(investigation_events.c.step_index > after_step)
        stmt = stmt.order_by(
            investigation_events.c.step_index.asc(),
            investigation_events.c.created_at.asc(),
        )

        rows = (await self.s.execute(stmt)).mappings().all()
        return [
            InvestigationEventV1(
                id=UUID(str(r["id"])),
                org_id=UUID(str(r["org_id"])),
                investigation_id=UUID(str(r["investigation_id"])),
                step_index=r["step_index"],
                event_type=r["event_type"],
                node_name=r["node_name"],
                payload=r["payload_json"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    async def complete_investigation(
        self,
        org_id: UUID,
        investigation_id: UUID,
        worker_id: str,
        fencing_token: int,
        verdict: str,
        verdict_confidence: float | None = None,
        summary: str | None = None,
    ) -> None:
        """Marks investigation complete if worker fence is valid."""
        now = datetime.now(UTC)
        stmt = (
            update(investigations)
            .where(
                investigations.c.org_id == org_id,
                investigations.c.id == investigation_id,
                investigations.c.worker_id == worker_id,
                investigations.c.fencing_token == fencing_token,
            )
            .values(
                status=InvestigationStatus.COMPLETED.value,
                verdict=verdict,
                verdict_confidence=verdict_confidence,
                summary=summary,
                worker_lease_expires_at=None,
                updated_at=now,
            )
        )
        res = await self.s.execute(stmt)
        rowcount = getattr(res, "rowcount", -1)
        if rowcount == 0:
            raise StaleWorkerFencingError(
                f"Cannot complete investigation: worker '{worker_id}' with fence {fencing_token} "
                "has been superseded"
            )

        await self.append_event(
            org_id=org_id,
            investigation_id=investigation_id,
            step_index=999,
            event_type=InvestigationEventType.VERDICT_REACHED.value,
            node_name="finalizer",
            payload={
                "verdict": verdict,
                "verdict_confidence": verdict_confidence,
                "summary": summary,
            },
        )

    async def fail_investigation(
        self,
        org_id: UUID,
        investigation_id: UUID,
        worker_id: str,
        fencing_token: int,
        failure_reason: str,
    ) -> None:
        """Marks investigation failed due to error, timeout, or budget limit."""
        now = datetime.now(UTC)
        stmt = (
            update(investigations)
            .where(
                investigations.c.org_id == org_id,
                investigations.c.id == investigation_id,
                investigations.c.worker_id == worker_id,
                investigations.c.fencing_token == fencing_token,
            )
            .values(
                status=InvestigationStatus.FAILED.value,
                failure_reason=failure_reason,
                worker_lease_expires_at=None,
                updated_at=now,
            )
        )
        res = await self.s.execute(stmt)
        rowcount = getattr(res, "rowcount", -1)
        if rowcount == 0:
            raise StaleWorkerFencingError(
                f"Cannot fail investigation: worker '{worker_id}' with fence {fencing_token} "
                "has been superseded"
            )

        await self.append_event(
            org_id=org_id,
            investigation_id=investigation_id,
            step_index=999,
            event_type=InvestigationEventType.FAILED.value,
            node_name="supervisor",
            payload={"failure_reason": failure_reason},
        )
