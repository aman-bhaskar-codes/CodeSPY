"""Durable investigation repositories for specs, checkpoints, reports, evidence and commands.

Provides scoped, transaction-local database adapters adhering to the
InvestigationExecutionCheckpointRepository port and Task B storage requirements.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import desc, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.investigation_execution import (
    InvestigationEvidenceV1,
    InvestigationExecutionCheckpointV1,
    InvestigationExecutionStateV1,
    InvestigationPublicationReceiptV1,
    InvestigationReportV1,
    InvestigationScopeV1,
    InvestigationSpecV1,
    compute_node_id,
)
from app.contracts.jobs import AttemptLeaseV2
from app.db.time import database_now
from app.domain.errors import DuplicateConflict, ScopeViolation
from app.models.investigation_execution_tables import (
    investigation_commands,
    investigation_evidence,
    investigation_execution_checkpoints,
    investigation_jobs,
    investigation_publication_receipts,
    investigation_reports,
    investigation_specs,
    investigation_step_receipts,
)
from app.models.investigation_tables import investigations
from app.ports.investigation_execution import (
    InvestigationExecutionCheckpointRepository,
)


def _ensure_utc(dt: datetime | None) -> datetime:
    if dt is None:
        return datetime.now(UTC)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


# ---------------------------------------------------------------------------
# Execution Checkpoints
# ---------------------------------------------------------------------------


class SqlInvestigationExecutionCheckpointRepository(InvestigationExecutionCheckpointRepository):
    """Job-backed versioned execution checkpoint repository."""

    async def load_latest(
        self,
        session: AsyncSession,
        scope: InvestigationScopeV1,
    ) -> InvestigationExecutionCheckpointV1 | None:
        stmt = (
            select(investigation_execution_checkpoints)
            .where(
                investigation_execution_checkpoints.c.org_id == scope.org_id,
                investigation_execution_checkpoints.c.investigation_id == scope.investigation_id,
                investigation_execution_checkpoints.c.execution_revision
                == scope.execution_revision,
            )
            .order_by(desc(investigation_execution_checkpoints.c.checkpoint_sequence))
            .limit(1)
        )
        row = (await session.execute(stmt)).mappings().one_or_none()
        if row is None:
            return None

        state_data = row["state_snapshot_json"]
        if isinstance(state_data, str):
            state_data = json.loads(state_data)

        state = InvestigationExecutionStateV1.model_validate(state_data, strict=False)
        return InvestigationExecutionCheckpointV1(
            org_id=row["org_id"],
            investigation_id=row["investigation_id"],
            execution_revision=row["execution_revision"],
            checkpoint_sequence=row["checkpoint_sequence"],
            state=state,
            state_digest=row["state_digest"],
            writer_job_id=row["writer_job_id"],
            writer_attempt_id=row["writer_attempt_id"],
            writer_fence=row["writer_fencing_token"],
            created_at=_ensure_utc(row["created_at"]),
        )

    async def append(
        self,
        session: AsyncSession,
        lease: AttemptLeaseV2,
        expected_previous_sequence: int,
        state: InvestigationExecutionStateV1,
    ) -> InvestigationExecutionCheckpointV1:
        # Validate state
        next_seq = expected_previous_sequence + 1
        persisted_state = (
            state
            if state.checkpoint_sequence == next_seq
            else state.model_copy(update={"checkpoint_sequence": next_seq})
        )
        state_digest = persisted_state.compute_state_digest()
        inv_id = state.investigation_id
        if inv_id is None:
            job_stmt = select(investigation_jobs.c.investigation_id).where(
                investigation_jobs.c.org_id == lease.org_id,
                investigation_jobs.c.id == lease.job_id,
            )
            inv_id = (await session.execute(job_stmt)).scalar_one_or_none()
            if inv_id is None:
                raise ScopeViolation(f"investigation not found for job {lease.job_id}")

        rev = state.execution_revision

        # Check existing row at target sequence (replay check)
        existing_stmt = select(investigation_execution_checkpoints).where(
            investigation_execution_checkpoints.c.org_id == lease.org_id,
            investigation_execution_checkpoints.c.investigation_id == inv_id,
            investigation_execution_checkpoints.c.execution_revision == rev,
            investigation_execution_checkpoints.c.checkpoint_sequence == next_seq,
        )
        existing = (await session.execute(existing_stmt)).mappings().one_or_none()
        if existing is not None:
            if existing["state_digest"] == state_digest:
                # Idempotent replay of identical checkpoint
                state_data = existing["state_snapshot_json"]
                if isinstance(state_data, str):
                    state_data = json.loads(state_data)
                return InvestigationExecutionCheckpointV1(
                    org_id=existing["org_id"],
                    investigation_id=existing["investigation_id"],
                    execution_revision=existing["execution_revision"],
                    checkpoint_sequence=existing["checkpoint_sequence"],
                    state=InvestigationExecutionStateV1.model_validate(state_data, strict=False),
                    state_digest=existing["state_digest"],
                    writer_job_id=existing["writer_job_id"],
                    writer_attempt_id=existing["writer_attempt_id"],
                    writer_fence=existing["writer_fencing_token"],
                    created_at=_ensure_utc(existing["created_at"]),
                )
            raise DuplicateConflict("checkpoint sequence replay conflict")

        # Verify predecessor
        if expected_previous_sequence > 0:
            pred_stmt = select(investigation_execution_checkpoints).where(
                investigation_execution_checkpoints.c.org_id == lease.org_id,
                investigation_execution_checkpoints.c.investigation_id == inv_id,
                investigation_execution_checkpoints.c.execution_revision == rev,
                investigation_execution_checkpoints.c.checkpoint_sequence
                == expected_previous_sequence,
            )
            pred = (await session.execute(pred_stmt)).mappings().one_or_none()
            if pred is None:
                raise ScopeViolation(
                    f"expected previous checkpoint {expected_previous_sequence} not found"
                )
        elif expected_previous_sequence == 0:
            pred_stmt = (
                select(investigation_execution_checkpoints.c.checkpoint_sequence)
                .where(
                    investigation_execution_checkpoints.c.org_id == lease.org_id,
                    investigation_execution_checkpoints.c.investigation_id == inv_id,
                    investigation_execution_checkpoints.c.execution_revision == rev,
                )
                .order_by(investigation_execution_checkpoints.c.checkpoint_sequence.asc())
                .limit(1)
            )
            first_seq = (await session.execute(pred_stmt)).scalar_one_or_none()
            if first_seq is not None and first_seq > 0:
                raise ScopeViolation("expected previous checkpoint 0 not found")

        now = await database_now(session)
        node_id = compute_node_id(
            lease.org_id,
            inv_id,
            state.spec_hash,
            state.next_node_kind.value,
            state.next_role,
            state.next_ordinal,
        )
        values = {
            "org_id": lease.org_id,
            "investigation_id": inv_id,
            "execution_revision": rev,
            "checkpoint_sequence": next_seq,
            "spec_hash": state.spec_hash,
            "node_id": node_id,
            "node_kind": state.next_node_kind.value,
            "role": state.next_role,
            "logical_ordinal": state.next_ordinal,
            "state_snapshot_json": persisted_state.model_dump(mode="json"),
            "state_digest": state_digest,
            "writer_job_id": lease.job_id,
            "writer_attempt_id": lease.attempt_id,
            "writer_fencing_token": lease.fence,
            "created_at": now,
        }
        await session.execute(insert(investigation_execution_checkpoints).values(**values))

        return InvestigationExecutionCheckpointV1(
            org_id=lease.org_id,
            investigation_id=inv_id,
            execution_revision=rev,
            checkpoint_sequence=next_seq,
            state=persisted_state,
            state_digest=state_digest,
            writer_job_id=lease.job_id,
            writer_attempt_id=lease.attempt_id,
            writer_fence=lease.fence,
            created_at=now,
        )


# ---------------------------------------------------------------------------
# Investigation Specs
# ---------------------------------------------------------------------------


class SqlInvestigationSpecRepository:
    """Immutable specification persistence."""

    async def save_spec(
        self,
        session: AsyncSession,
        spec: InvestigationSpecV1,
    ) -> InvestigationSpecV1:
        spec_hash = spec.compute_spec_hash()

        # Check existing
        existing = await self.get_spec(session, spec.org_id, spec_hash)
        if existing is not None:
            return existing

        spec_id = uuid4()
        values = {
            "org_id": spec.org_id,
            "id": spec_id,
            "spec_hash": spec_hash,
            "repository_id": spec.repository_id,
            "snapshot_id": spec.snapshot_id,
            "principal_id": spec.principal_id,
            "target_type": spec.target_type,
            "finding_id": spec.finding_id,
            "finding_run_id": spec.finding_run_id,
            "chunk_set_id": spec.chunk_set_id,
            "chunk_config_id": spec.chunk_config_id,
            "materialization_id": spec.materialization_id,
            "query": spec.query,
            "mode": spec.mode,
            "depth": spec.depth,
            "limits_json": spec.limits.model_dump(mode="json"),
            "disclosure_policy_digest": spec.disclosure_policy_digest,
            "tool_policy_revision": spec.tool_policy_revision,
            "model_route_revision": spec.model_route_revision,
            "model_profile_revision": spec.model_profile_revision,
            "runtime_revision": spec.runtime_revision,
            "privacy_policy_revision": spec.privacy_policy_revision,
            "created_at": await database_now(session),
        }
        await session.execute(insert(investigation_specs).values(**values))
        return spec

    async def get_spec(
        self,
        session: AsyncSession,
        org_id: UUID,
        spec_hash: str,
    ) -> InvestigationSpecV1 | None:
        stmt = select(investigation_specs).where(
            investigation_specs.c.org_id == org_id,
            investigation_specs.c.spec_hash == spec_hash,
        )
        row = (await session.execute(stmt)).mappings().one_or_none()
        if row is None:
            return None

        limits_data = row["limits_json"]
        if isinstance(limits_data, str):
            limits_data = json.loads(limits_data)

        return InvestigationSpecV1(
            org_id=row["org_id"],
            repository_id=row["repository_id"],
            snapshot_id=row["snapshot_id"],
            principal_id=row["principal_id"],
            target_type=row["target_type"],
            finding_id=row["finding_id"],
            finding_run_id=row["finding_run_id"],
            chunk_set_id=row["chunk_set_id"],
            chunk_config_id=row["chunk_config_id"],
            materialization_id=row["materialization_id"],
            query=row["query"],
            mode=row["mode"],
            depth=row["depth"],
            limits=limits_data,
            disclosure_policy_digest=row["disclosure_policy_digest"],
            tool_policy_revision=row["tool_policy_revision"],
            model_route_revision=row["model_route_revision"],
            model_profile_revision=row["model_profile_revision"],
            runtime_revision=row["runtime_revision"],
            privacy_policy_revision=row["privacy_policy_revision"],
        )

    get_spec_by_hash = get_spec


# ---------------------------------------------------------------------------
# Investigation Commands
# ---------------------------------------------------------------------------


class SqlInvestigationCommandRepository:
    """Idempotent command receipts."""

    async def replay(
        self,
        session: AsyncSession,
        org_id: UUID,
        principal_id: UUID,
        command_kind: str,
        idempotency_key: str,
        request_digest: str,
    ) -> dict[str, Any] | None:
        stmt = select(investigation_commands).where(
            investigation_commands.c.org_id == org_id,
            investigation_commands.c.command_kind == command_kind,
            investigation_commands.c.idempotency_key == idempotency_key,
        )
        row = (await session.execute(stmt)).mappings().one_or_none()
        if row is None:
            return None
        if row["request_digest"] != request_digest or row["principal_id"] != principal_id:
            raise DuplicateConflict(
                f"idempotency key {idempotency_key} conflicts with prior request"
            )
        resp = row["response_json"]
        if isinstance(resp, str):
            resp = json.loads(resp)
        return dict(resp)

    async def record(
        self,
        session: AsyncSession,
        org_id: UUID,
        command_kind: str,
        idempotency_key: str,
        request_digest: str,
        principal_id: UUID,
        investigation_id: UUID,
        job_id: UUID | None,
        status_code: int,
        response_json: dict[str, Any],
    ) -> None:
        values = {
            "org_id": org_id,
            "id": uuid4(),
            "command_kind": command_kind,
            "idempotency_key": idempotency_key,
            "request_digest": request_digest,
            "principal_id": principal_id,
            "investigation_id": investigation_id,
            "job_id": job_id,
            "status_code": status_code,
            "response_json": response_json,
            "created_at": await database_now(session),
        }
        await session.execute(insert(investigation_commands).values(**values))


# ---------------------------------------------------------------------------
# Investigation Reports & Publication Receipts
# ---------------------------------------------------------------------------


class SqlInvestigationReportRepository:
    """Immutable final report publication and atomic receipts."""

    async def publish_report(
        self,
        session: AsyncSession,
        report: InvestigationReportV1,
        job_id: UUID,
        attempt_id: UUID,
        fencing_token: int,
        draft_digest: str,
    ) -> InvestigationPublicationReceiptV1:
        # Lock investigation
        inv_stmt = (
            select(investigations)
            .where(
                investigations.c.id == report.investigation_id,
            )
            .with_for_update()
        )
        inv_row = (await session.execute(inv_stmt)).mappings().one_or_none()
        if inv_row is None:
            raise ScopeViolation("investigation not found")

        org_id = inv_row["org_id"]
        report_digest = report.compute_report_digest()
        now = await database_now(session)

        # Check existing report for this job
        existing_report_stmt = select(investigation_reports).where(
            investigation_reports.c.org_id == org_id,
            investigation_reports.c.job_id == job_id,
        )
        existing_report = (await session.execute(existing_report_stmt)).mappings().one_or_none()
        if existing_report is not None:
            if existing_report["report_digest"] != report_digest:
                raise DuplicateConflict("report publication digest conflict")
            report_id = existing_report["id"]
        else:
            report_id = report.report_id
            report_values = {
                "org_id": org_id,
                "id": report_id,
                "investigation_id": report.investigation_id,
                "spec_hash": report.spec_hash,
                "execution_revision": report.runtime_revision,
                "job_id": job_id,
                "verdict": report.verdict,
                "verdict_confidence": report.verdict_confidence,
                "summary": report.summary,
                "report_json": report.model_dump(mode="json"),
                "report_digest": report_digest,
                "claims_count": len(report.claims),
                "evidence_refs_count": len(report.evidence_refs),
                "created_at": now,
            }
            await session.execute(insert(investigation_reports).values(**report_values))

        # Check existing publication receipt
        existing_receipt_stmt = select(investigation_publication_receipts).where(
            investigation_publication_receipts.c.org_id == org_id,
            investigation_publication_receipts.c.job_id == job_id,
        )
        existing_receipt = (await session.execute(existing_receipt_stmt)).mappings().one_or_none()
        if existing_receipt is not None:
            receipt_id = existing_receipt["id"]
        else:
            receipt_id = uuid4()
            receipt_values = {
                "org_id": org_id,
                "id": receipt_id,
                "investigation_id": report.investigation_id,
                "spec_hash": report.spec_hash,
                "job_id": job_id,
                "attempt_id": attempt_id,
                "fencing_token": fencing_token,
                "report_id": report_id,
                "report_digest": report_digest,
                "draft_digest": draft_digest,
                "published_at": now,
            }
            await session.execute(
                insert(investigation_publication_receipts).values(**receipt_values)
            )

        # Update investigation terminal state
        await session.execute(
            update(investigations)
            .where(
                investigations.c.org_id == org_id,
                investigations.c.id == report.investigation_id,
            )
            .values(
                final_report_id=report_id,
                verdict=report.verdict,
                verdict_confidence=report.verdict_confidence,
                summary=report.summary,
                status="completed",
                updated_at=now,
            )
        )

        return InvestigationPublicationReceiptV1(
            receipt_id=receipt_id,
            investigation_id=report.investigation_id,
            job_id=job_id,
            attempt_id=attempt_id,
            attempt_fence=fencing_token,
            spec_hash=report.spec_hash,
            report_id=report_id,
            report_digest=report_digest,
            draft_digest=draft_digest,
            created_at=now,
        )

    async def get_report(
        self,
        session: AsyncSession,
        org_id: UUID,
        report_id: UUID,
    ) -> InvestigationReportV1 | None:
        stmt = select(investigation_reports).where(
            investigation_reports.c.org_id == org_id,
            investigation_reports.c.id == report_id,
        )
        row = (await session.execute(stmt)).mappings().one_or_none()
        if row is None:
            return None
        data = row["report_json"]
        if isinstance(data, str):
            data = json.loads(data)
        return InvestigationReportV1.model_validate(data, strict=False)

    async def get_report_by_job(
        self,
        session: AsyncSession,
        org_id: UUID,
        job_id: UUID,
    ) -> InvestigationReportV1 | None:
        stmt = select(investigation_reports).where(
            investigation_reports.c.org_id == org_id,
            investigation_reports.c.job_id == job_id,
        )
        row = (await session.execute(stmt)).mappings().one_or_none()
        if row is None:
            return None
        data = row["report_json"]
        if isinstance(data, str):
            data = json.loads(data)
        return InvestigationReportV1.model_validate(data, strict=False)

    async def get_publication_receipt(
        self,
        session: AsyncSession,
        org_id: UUID,
        job_id: UUID,
    ) -> InvestigationPublicationReceiptV1 | None:
        stmt = select(investigation_publication_receipts).where(
            investigation_publication_receipts.c.org_id == org_id,
            investigation_publication_receipts.c.job_id == job_id,
        )
        row = (await session.execute(stmt)).mappings().one_or_none()
        if row is None:
            return None
        return InvestigationPublicationReceiptV1(
            receipt_id=row["id"],
            investigation_id=row["investigation_id"],
            job_id=row["job_id"],
            attempt_id=row["attempt_id"],
            attempt_fence=row["fencing_token"],
            spec_hash=row["spec_hash"],
            report_id=row["report_id"],
            report_digest=row["report_digest"],
            draft_digest=row["draft_digest"],
            created_at=_ensure_utc(row["published_at"]),
        )


# ---------------------------------------------------------------------------
# Investigation Evidence
# ---------------------------------------------------------------------------


class SqlInvestigationEvidenceRepository:
    """Immutable evidence observations."""

    async def record_evidence(
        self,
        session: AsyncSession,
        evidence: InvestigationEvidenceV1,
    ) -> UUID:
        values = {
            "org_id": evidence.org_id,
            "id": evidence.id,
            "investigation_id": evidence.investigation_id,
            "snapshot_id": evidence.snapshot_id,
            "source_path": evidence.source_path,
            "file_sha256": evidence.file_sha256,
            "original_start_line": evidence.original_start_line,
            "original_end_line": evidence.original_end_line,
            "safe_start_line": evidence.safe_start_line,
            "safe_end_line": evidence.safe_end_line,
            "excerpt_sha256": evidence.excerpt_sha256,
            "observation_type": evidence.observation_type,
            "coverage_summary": evidence.coverage_summary,
            "limitations": evidence.limitations,
            "tool_receipt_id": evidence.tool_receipt_id,
            "created_at": evidence.created_at or await database_now(session),
        }
        await session.execute(insert(investigation_evidence).values(**values))
        return evidence.id

    async def list_evidence(
        self,
        session: AsyncSession,
        org_id: UUID,
        investigation_id: UUID,
    ) -> list[InvestigationEvidenceV1]:
        stmt = (
            select(investigation_evidence)
            .where(
                investigation_evidence.c.org_id == org_id,
                investigation_evidence.c.investigation_id == investigation_id,
            )
            .order_by(investigation_evidence.c.created_at)
        )
        rows = (await session.execute(stmt)).mappings().all()
        return [
            InvestigationEvidenceV1(
                id=r["id"],
                org_id=r["org_id"],
                investigation_id=r["investigation_id"],
                snapshot_id=r["snapshot_id"],
                source_path=r["source_path"],
                file_sha256=r["file_sha256"],
                original_start_line=r["original_start_line"],
                original_end_line=r["original_end_line"],
                safe_start_line=r["safe_start_line"],
                safe_end_line=r["safe_end_line"],
                excerpt_sha256=r["excerpt_sha256"],
                observation_type=r["observation_type"],
                coverage_summary=r["coverage_summary"] or "",
                limitations=r["limitations"] or "",
                tool_receipt_id=r["tool_receipt_id"],
                created_at=_ensure_utc(r["created_at"]),
            )
            for r in rows
        ]


# ---------------------------------------------------------------------------
# Investigation Step Receipts
# ---------------------------------------------------------------------------


class SqlInvestigationStepReceiptRepository:
    """Per-node execution step receipts."""

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
        values = {
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
            "created_at": await database_now(session),
        }
        await session.execute(insert(investigation_step_receipts).values(**values))

    async def get_receipt(
        self,
        session: AsyncSession,
        org_id: UUID,
        investigation_id: UUID,
        execution_revision: int,
        node_id: UUID,
    ) -> dict[str, Any] | None:
        stmt = select(investigation_step_receipts).where(
            investigation_step_receipts.c.org_id == org_id,
            investigation_step_receipts.c.investigation_id == investigation_id,
            investigation_step_receipts.c.execution_revision == execution_revision,
            investigation_step_receipts.c.node_id == node_id,
        )
        row = (await session.execute(stmt)).mappings().one_or_none()
        return dict(row) if row is not None else None
