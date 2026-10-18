from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.evidence import EvidenceEnvelopeV1, EvidenceRecord, EvidenceRef
from app.domain.errors import DuplicateConflict, ScopeViolation
from app.domain.scope import RunRef, TenantContext
from app.models.tables import evidence_records
from app.repositories.analysis_repository import AnalysisRepository


class EvidenceRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def insert_batch(
        self, ctx: TenantContext, ref: RunRef, records: list[EvidenceEnvelopeV1]
    ) -> list[EvidenceRef]:
        await AnalysisRepository(self.session).get(ctx, ref, lock=True)
        if len(records) > 1000:
            raise ValueError("evidence batch exceeds bound")
        result = []
        for record in records:
            if record.scope != ref:
                raise ScopeViolation("evidence scope differs")
            data = record.model_dump(exclude={"scope", "range"})
            data.update(
                org_id=ref.org_id,
                snapshot_id=ref.snapshot_id,
                run_id=ref.run_id,
                digest=record.semantic_hash(),
            )
            if record.range:
                data.update(record.range.model_dump())
            existing = (
                (
                    await self.session.execute(
                        select(evidence_records).where(
                            evidence_records.c.org_id == ref.org_id,
                            evidence_records.c.snapshot_id == ref.snapshot_id,
                            evidence_records.c.run_id == ref.run_id,
                            evidence_records.c.id == record.id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing:
                if existing["digest"] != data["digest"]:
                    raise DuplicateConflict("evidence identity differs")
            else:
                await self.session.execute(insert(evidence_records).values(**data))
            result.append(EvidenceRef(scope=ref, evidence_id=record.id))
        return result

    async def read_evidence(self, ctx: TenantContext, ref: EvidenceRef) -> EvidenceRecord:
        await AnalysisRepository(self.session).get(ctx, ref.scope, published=True)
        row = (
            (
                await self.session.execute(
                    select(evidence_records).where(
                        evidence_records.c.org_id == ref.scope.org_id,
                        evidence_records.c.snapshot_id == ref.scope.snapshot_id,
                        evidence_records.c.run_id == ref.scope.run_id,
                        evidence_records.c.id == ref.evidence_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if not row:
            raise ScopeViolation("evidence unavailable")
        data = dict(row)
        data["scope"] = ref.scope
        names = ["start_byte", "end_byte", "start_line", "end_line", "start_col", "end_col"]
        data["range"] = {n: data.pop(n) for n in names}
        if data["range"]["start_byte"] is None:
            data["range"] = None
        for n in ["org_id", "snapshot_id", "run_id"]:
            data.pop(n)
        return EvidenceRecord(**data)
