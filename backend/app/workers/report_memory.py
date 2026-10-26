"""Bounded local outbox effects; report publication and financial history stay independent."""

import logging
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from app.contracts.investigation_execution import InvestigationEvidenceV1, InvestigationReportV1
from app.contracts.memory import EvidencePointerV1
from app.db.time import database_now
from app.db.transaction import transaction
from app.domain.canonical_json import semantic_digest
from app.domain.errors import InvalidTransition, ScopeViolation
from app.models.investigation_execution_tables import investigation_evidence, investigation_reports
from app.models.investigation_tables import investigations
from app.models.phase2_tables import PHASE2_TABLES
from app.models.report_memory_tables import report_memory_receipts
from app.ports.scan_execution import ScanCapabilities
from app.ports.source_reader import SourceReaderPort
from app.security.authorization import require_repository
from app.services.analysis_sources import lock_retained_snapshot
from app.services.memory_evidence import SourceMemoryEvidence
from app.services.memory_service import MemoryService
from app.services.report_memory import derive_report_memories
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import ColumnElement, and_, func, insert, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)
outbox = PHASE2_TABLES["outbox"]


class MemoryProposalIntentV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: int = Field(strict=True)
    org_id: UUID
    repository_id: UUID
    snapshot_id: UUID
    investigation_id: UUID
    report_id: UUID
    report_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    derivation_revision: int = Field(strict=True)


class PrevalidatedEvidence:
    """Current checks only after the exact pointers passed full isolated-source validation."""

    def __init__(self, evidence: SourceMemoryEvidence, allowed: set[str]):
        self.evidence, self.allowed = evidence, allowed

    async def verify(
        self, org: UUID, repo: UUID, snapshot: UUID, actor: UUID, pointer: EvidencePointerV1
    ) -> None:
        if (
            semantic_digest(
                [str(org), str(repo), str(snapshot), str(actor), pointer.model_dump(mode="json")]
            )
            not in self.allowed
        ):
            raise ScopeViolation("report memory evidence was not validated")
        await self.evidence.verify_current(org, repo, snapshot, actor, pointer)

    async def verify_current(
        self, org: UUID, repo: UUID, snapshot: UUID, actor: UUID, pointer: EvidencePointerV1
    ) -> None:
        await self.verify(org, repo, snapshot, actor, pointer)


class ReportMemoryProposalWorker:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        reader: SourceReaderPort | None = None,
        capabilities: ScanCapabilities | None = None,
    ):
        self.factory, self.reader, self.capabilities = factory, reader, capabilities

    async def tick(self, limit: int = 20) -> int:
        if not 1 <= limit <= 20:
            raise ValueError("invalid report memory batch")
        async with transaction(self.factory) as session:
            if (
                session.bind is not None
                and session.bind.dialect.name == "postgresql"
                and await session.scalar(
                    text("SELECT to_regclass('public.report_memory_receipts')")
                )
                is None
            ):
                return 0
            rows = (
                (
                    await session.execute(
                        select(outbox)
                        .where(
                            outbox.c.kind == "memory_proposals",
                            outbox.c.next_send_at <= func.clock_timestamp(),
                            or_(
                                outbox.c.state == "pending",
                                (outbox.c.state == "claimed")
                                & (outbox.c.claim_expires_at <= func.clock_timestamp()),
                            ),
                        )
                        .order_by(outbox.c.next_send_at, outbox.c.org_id, outbox.c.id)
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                )
                .mappings()
                .all()
            )
            claimed = []
            for row in rows:
                token = uuid4().hex
                await session.execute(
                    update(outbox)
                    .where(outbox.c.org_id == row["org_id"], outbox.c.id == row["id"])
                    .values(
                        state="claimed",
                        claim_token=token,
                        claim_expires_at=await database_now(session) + timedelta(seconds=60),
                        send_attempts=row["send_attempts"] + 1,
                    )
                )
                claimed.append((dict(row), token))
        completed = 0
        for claim, token in claimed:
            try:
                await self._process(claim, token)
                completed += 1
            except ValidationError:
                completed += int(await self._finish(claim, token, "invalid_origin"))
            except (ScopeViolation, InvalidTransition):
                completed += int(await self._finish(claim, token, "access_or_source_unavailable"))
            except Exception:
                logger.warning("report_memory_processing_unavailable")
                if claim["send_attempts"] >= 2:
                    completed += int(await self._finish(claim, token, "processing_failed"))
                else:
                    async with transaction(self.factory) as session:
                        await session.execute(
                            update(outbox)
                            .where(self._owned(claim, token))
                            .values(
                                state="pending",
                                claim_token=None,
                                claim_expires_at=None,
                                next_send_at=await database_now(session) + timedelta(seconds=30),
                            )
                        )
        return completed

    @staticmethod
    def _owned(row: dict[str, Any], token: str) -> ColumnElement[bool]:
        return and_(
            outbox.c.org_id == row["org_id"],
            outbox.c.id == row["id"],
            outbox.c.state == "claimed",
            outbox.c.claim_token == token,
        )

    async def _process(self, row: dict[str, Any], token: str) -> None:
        intent = MemoryProposalIntentV1.model_validate(row["payload"])
        if (
            intent.schema_version != 1
            or intent.derivation_revision != 1
            or intent.org_id != row["org_id"]
        ):
            await self._finish(row, token, "invalid_origin")
            return
        async with self.factory() as session:
            origin = (
                (
                    await session.execute(
                        select(
                            investigation_reports,
                            investigations.c.repository_id,
                            investigations.c.snapshot_id,
                            investigations.c.principal_id,
                        )
                        .join(
                            investigations,
                            (investigations.c.org_id == investigation_reports.c.org_id)
                            & (investigations.c.id == investigation_reports.c.investigation_id),
                        )
                        .where(
                            investigation_reports.c.org_id == row["org_id"],
                            investigation_reports.c.job_id == row["job_id"],
                            investigation_reports.c.id == intent.report_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if (
                origin is None
                or origin["report_digest"] != intent.report_digest
                or origin["repository_id"] != intent.repository_id
                or origin["snapshot_id"] != intent.snapshot_id
                or origin["investigation_id"] != intent.investigation_id
            ):
                await self._finish(row, token, "invalid_origin")
                return
            await require_repository(
                session, row["org_id"], origin["principal_id"], intent.repository_id, write=True
            )
            report = InvestigationReportV1.model_validate(origin["report_json"], strict=False)
            if report.compute_report_digest() != intent.report_digest:
                raise ScopeViolation("report memory digest differs")
            evidence = (
                (
                    await session.execute(
                        select(investigation_evidence)
                        .where(
                            investigation_evidence.c.org_id == row["org_id"],
                            investigation_evidence.c.investigation_id == intent.investigation_id,
                            investigation_evidence.c.id.in_(report.evidence_refs),
                        )
                        .order_by(investigation_evidence.c.id)
                        .limit(201)
                    )
                )
                .mappings()
                .all()
            )
            records = tuple(
                InvestigationEvidenceV1.model_validate(dict(e), strict=False) for e in evidence
            )
            proposals = derive_report_memories(
                report,
                records,
                row["org_id"],
                intent.repository_id,
                intent.snapshot_id,
                origin["principal_id"],
            )
            proof = SourceMemoryEvidence(session, self.factory, self.reader, self.capabilities)
            allowed = set()
            # Source extraction holds no outbox, repository or job locks.
            for proposal in proposals:
                for pointer in proposal.evidence_pointers:
                    await proof.verify(
                        row["org_id"],
                        intent.repository_id,
                        intent.snapshot_id,
                        origin["principal_id"],
                        pointer,
                    )
                    allowed.add(
                        semantic_digest(
                            [
                                str(row["org_id"]),
                                str(intent.repository_id),
                                str(intent.snapshot_id),
                                str(origin["principal_id"]),
                                pointer.model_dump(mode="json"),
                            ]
                        )
                    )
        async with transaction(self.factory) as session:
            owned = await session.scalar(
                select(outbox.c.id).where(self._owned(row, token)).with_for_update()
            )
            if owned is None:
                return
            existing = await session.scalar(
                select(report_memory_receipts.c.intent_id).where(
                    report_memory_receipts.c.org_id == row["org_id"],
                    report_memory_receipts.c.report_id == intent.report_id,
                    report_memory_receipts.c.derivation_revision == 1,
                )
            )
            if existing is not None:
                await self._ack(session, row, token)
                return
            await require_repository(
                session, row["org_id"], origin["principal_id"], intent.repository_id, write=True
            )
            await lock_retained_snapshot(
                session, row["org_id"], intent.repository_id, intent.snapshot_id
            )
            proof = SourceMemoryEvidence(session, self.factory, self.reader, self.capabilities)
            service = MemoryService(session, evidence=PrevalidatedEvidence(proof, allowed))
            ids = []
            for proposal in proposals:
                entry = await service.record_memory(proposal)
                ids.append(str(entry.memory_id))
            await session.execute(
                insert(report_memory_receipts).values(
                    org_id=row["org_id"],
                    intent_id=row["id"],
                    report_id=intent.report_id,
                    report_digest=intent.report_digest,
                    derivation_revision=1,
                    outcome="created" if ids else "no_candidates",
                    omitted_claims=len(report.claims) - len(proposals),
                    reason="unsupported_or_capped" if len(report.claims) > len(proposals) else None,
                    candidate_ids=ids,
                )
            )
            await self._ack(session, row, token)

    async def _finish(self, row: dict[str, Any], token: str, reason: str) -> bool:
        async with transaction(self.factory) as session:
            owned = await session.scalar(
                select(outbox.c.id).where(self._owned(row, token)).with_for_update()
            )
            if owned is None:
                return False
            existing = await session.scalar(
                select(report_memory_receipts.c.intent_id).where(
                    report_memory_receipts.c.org_id == row["org_id"],
                    report_memory_receipts.c.intent_id == row["id"],
                )
            )
            if existing is None:
                await session.execute(
                    insert(report_memory_receipts).values(
                        org_id=row["org_id"],
                        intent_id=row["id"],
                        derivation_revision=1,
                        outcome="blocked",
                        reason=reason,
                        candidate_ids=[],
                    )
                )
            await self._ack(session, row, token)
            return True

    @staticmethod
    async def _ack(session: AsyncSession, row: dict[str, Any], token: str) -> None:
        await session.execute(
            update(outbox)
            .where(ReportMemoryProposalWorker._owned(row, token))
            .values(
                state="sent",
                sent_at=await database_now(session),
                claim_token=None,
                claim_expires_at=None,
            )
        )
