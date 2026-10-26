"""Append-only provider evidence; financial settlement stays in the existing ledger."""

from uuid import UUID, uuid4

from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.model_transport import ProviderUsageEvidenceV1
from app.domain.canonical_json import semantic_digest
from app.models.model_transport_tables import provider_usage_receipts


async def record_usage_receipt(
    session: AsyncSession,
    org_id: UUID,
    repository_id: UUID,
    reservation_id: UUID,
    profile_digest: str,
    evidence: ProviderUsageEvidenceV1,
) -> UUID:
    payload = evidence.model_dump(mode="json")
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        import json

        identity = await session.scalar(
            text(
                "SELECT public.codespy_record_model_usage("
                ":org,:repo,:res,:profile,:digest,CAST(:evidence AS jsonb))"
            ),
            {
                "org": org_id,
                "repo": repository_id,
                "res": reservation_id,
                "profile": profile_digest,
                "digest": semantic_digest(payload),
                "evidence": json.dumps(payload),
            },
        )
        return UUID(str(identity))
    digest = semantic_digest(payload)
    prior = await session.scalar(
        select(provider_usage_receipts.c.id).where(
            provider_usage_receipts.c.org_id == org_id,
            provider_usage_receipts.c.reservation_id == reservation_id,
            provider_usage_receipts.c.payload_digest == digest,
        )
    )
    if prior is not None:
        return UUID(str(prior))
    identity = uuid4()
    await session.execute(
        insert(provider_usage_receipts).values(
            org_id=org_id,
            id=identity,
            repository_id=repository_id,
            reservation_id=reservation_id,
            profile_digest=profile_digest,
            payload_digest=digest,
            quality=evidence.quality,
            evidence_json=payload,
        )
    )
    return identity
