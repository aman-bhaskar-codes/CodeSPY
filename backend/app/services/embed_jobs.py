# ruff: noqa: E501
"""Principal-bound requests to embed a sealed lexical chunk set into a dense index."""

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.embedding_index import EmbedConfigV1, EmbedReceiptV1, EmbedSpecV1
from app.contracts.embedding_jobs import (
    EmbedCreateRequestV1,
    EmbedCreateResponseV1,
    EmbedStatusV1,
    EnqueueEmbedJobV7,
)
from app.contracts.provider_budget import DisclosurePolicyV1
from app.db.transaction import transaction
from app.domain.errors import DuplicateConflict, ScopeViolation
from app.domain.scope import TenantContext
from app.repositories.job_repository import embed_job_record, load_job
from app.security.authorization import lock_scope, require_snapshot
from app.services.jobs import TERMINAL, JobService
from app.services.provider_budget import DisclosureDeniedError

EMBED_REGION = "ap-south-1"


class EmbedUnavailable(Exception):  # noqa: N818
    """The request is well formed but cannot be served now (no sealed set, no price)."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


async def _rows(session: AsyncSession, query: str, **params: Any) -> list[Any]:
    return list((await session.execute(text(query), params)).all())


class EmbedJobService:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory, self.jobs = factory, JobService(factory)

    async def create(
        self,
        ctx: TenantContext,
        repository_id: UUID,
        snapshot_id: UUID,
        chunk_set_id: UUID,
        config: EmbedConfigV1,
        request_key: str,
        *,
        policy: DisclosurePolicyV1 | None = None,
    ) -> EmbedCreateResponseV1:
        config = EmbedConfigV1.model_validate(config.model_dump()).validated()
        policy = policy or DisclosurePolicyV1()
        if not policy.is_provider_permitted(config.provider) or not policy.is_region_permitted(
            EMBED_REGION
        ):
            raise DisclosureDeniedError("provider or region is not permitted for this repository")
        request = EmbedCreateRequestV1(
            chunk_set_id=chunk_set_id, config=config, request_key=request_key
        )
        spec = EmbedSpecV1(
            org_id=ctx.org_id,
            repository_id=repository_id,
            snapshot_id=snapshot_id,
            principal_id=ctx.principal_id,
            chunk_set_id=request.chunk_set_id,
            config=config,
        )
        async with transaction(self.factory) as session:
            await lock_scope(session, ctx.org_id, ctx.principal_id, repository_id)
            await require_snapshot(session, ctx, repository_id, snapshot_id, write=True)
            replay = await self._replay(session, ctx, spec, request_key)
            if replay is not None:
                return EmbedCreateResponseV1(job=embed_job_record(replay))
            await self._require_servable(session, spec)
            spec_id = await self._spec(session, spec)
            job = await self.jobs._enqueue(
                session,
                ctx,
                EnqueueEmbedJobV7(
                    repository_id=repository_id,
                    snapshot_id=snapshot_id,
                    embed_spec_id=spec_id,
                    config_hash=config.semantic_hash(),
                    request_key=request_key,
                ),
            )
            return EmbedCreateResponseV1(job=embed_job_record(job))

    async def _replay(
        self, session: AsyncSession, ctx: TenantContext, spec: EmbedSpecV1, key: str
    ) -> dict[str, Any] | None:
        found = await _rows(
            session,
            "SELECT id FROM jobs WHERE org_id=:o AND request_key=:k",
            o=ctx.org_id,
            k=key,
        )
        if not found:
            return None
        old = await load_job(session, ctx.org_id, found[0].id)
        spec_row = (
            await _rows(
                session,
                "SELECT chunk_set_id,provider,model_id,dimension,normalize,price_version "
                "FROM embed_specs WHERE org_id=:o AND id=:s",
                o=ctx.org_id,
                s=old.get("embed_spec_id"),
            )
            if old["operation"] == "embed_chunk_set"
            else []
        )
        c = spec.config
        if (
            old["operation"] != "embed_chunk_set"
            or old["principal_id"] != ctx.principal_id
            or old["repository_id"] != spec.repository_id
            or old["snapshot_id"] != spec.snapshot_id
            or old["retry_of_job_id"] is not None
            or old["config_hash"] != c.semantic_hash()
            or not spec_row
            or (
                spec_row[0].chunk_set_id,
                spec_row[0].provider,
                spec_row[0].model_id,
                spec_row[0].dimension,
                spec_row[0].normalize,
                spec_row[0].price_version,
            )
            != (
                spec.chunk_set_id,
                c.provider,
                c.model_id,
                c.dimension,
                c.normalize,
                c.price_version,
            )
        ):
            raise DuplicateConflict("request key has different input")
        return old

    async def _require_servable(self, session: AsyncSession, spec: EmbedSpecV1) -> None:
        sealed = await _rows(
            session,
            "SELECT 1 FROM retrieval_chunk_sets s "
            "JOIN index_publication_receipts r ON r.org_id=s.org_id AND r.chunk_set_id=s.id "
            "JOIN search_indices i ON i.org_id=s.org_id AND i.id=r.index_id AND i.chunk_set_id=s.id "
            "WHERE s.org_id=:o AND s.id=:set AND s.repository_id=:repo AND s.snapshot_id=:snap "
            "AND i.index_type='lexical' AND i.state IN ('ready','partial_ready') AND s.chunk_count>0",
            o=spec.org_id,
            set=spec.chunk_set_id,
            repo=spec.repository_id,
            snap=spec.snapshot_id,
        )
        if not sealed:
            raise ScopeViolation("chunk set unavailable")
        c = spec.config
        priced = await _rows(
            session,
            "SELECT 1 FROM price_schedules WHERE provider=:p AND model_id=:m AND version=:v "
            "AND effective_at<=clock_timestamp() AND expires_at>clock_timestamp()",
            p=c.provider,
            m=c.model_id,
            v=c.price_version,
        )
        if not priced:
            raise EmbedUnavailable("embed_price_unavailable")
        published = await _rows(
            session,
            "SELECT 1 FROM search_indices WHERE org_id=:o AND id=:i",
            o=spec.org_id,
            i=spec.dense_index_id(),
        )
        if published:
            raise EmbedUnavailable("embed_already_published")
        collision = await _rows(
            session,
            "SELECT 1 FROM embed_specs WHERE org_id=:o AND chunk_set_id=:set AND provider=:p "
            "AND model_id=:m AND dimension=:d AND (normalize<>:n OR price_version<>:v)",
            o=spec.org_id,
            set=spec.chunk_set_id,
            p=c.provider,
            m=c.model_id,
            d=c.dimension,
            n=c.normalize,
            v=c.price_version,
        )
        if collision:
            # One dense index exists per (provider, model, dimension); a variant would collide.
            raise EmbedUnavailable("embed_profile_conflict")

    async def _spec(self, session: AsyncSession, spec: EmbedSpecV1) -> UUID:
        c = spec.config
        existing = await _rows(
            session,
            "SELECT id FROM embed_specs WHERE org_id=:o AND chunk_set_id=:set AND provider=:p "
            "AND model_id=:m AND dimension=:d AND normalize=:n AND price_version=:v",
            o=spec.org_id,
            set=spec.chunk_set_id,
            p=c.provider,
            m=c.model_id,
            d=c.dimension,
            n=c.normalize,
            v=c.price_version,
        )
        if existing:
            return UUID(str(existing[0].id))
        spec_id = uuid4()
        await session.execute(
            text(
                "INSERT INTO embed_specs(org_id,id,repository_id,snapshot_id,principal_id,chunk_set_id,"
                "lexical_index_id,provider,model_id,dimension,normalize,price_version,config_hash,"
                "spec_hash,config_json,spec_json,canonical_text) VALUES(:o,:id,:repo,:snap,:pr,:set,"
                ":lex,:p,:m,:d,:n,:v,:ch,:sh,CAST(:cj AS jsonb),CAST(:sj AS jsonb),:ct)"
            ),
            {
                "o": spec.org_id,
                "id": spec_id,
                "repo": spec.repository_id,
                "snap": spec.snapshot_id,
                "pr": spec.principal_id,
                "set": spec.chunk_set_id,
                "lex": spec.lexical_index_id(),
                "p": c.provider,
                "m": c.model_id,
                "d": c.dimension,
                "n": c.normalize,
                "v": c.price_version,
                "ch": c.semantic_hash(),
                "sh": spec.spec_hash(),
                "cj": _dump(c.model_dump(mode="json")),
                "sj": spec.canonical_text(),
                "ct": spec.canonical_text(),
            },
        )
        return spec_id

    async def get(
        self, ctx: TenantContext, repository_id: UUID, snapshot_id: UUID, job_id: UUID
    ) -> EmbedStatusV1:
        async with transaction(self.factory) as session:
            await require_snapshot(session, ctx, repository_id, snapshot_id)
            row = await load_job(session, ctx.org_id, job_id)
            if (
                row["operation"] != "embed_chunk_set"
                or row["repository_id"] != repository_id
                or row["snapshot_id"] != snapshot_id
            ):
                raise ScopeViolation("embed job unavailable")
            receipt = None
            if row["state"] in {"succeeded", "partial"}:
                found = await _rows(
                    session,
                    "SELECT job_id,spec_id,chunk_set_id,index_id,chunk_count,token_total,coverage,"
                    "embedding_digest,ledger_digest FROM embed_publication_receipts "
                    "WHERE org_id=:o AND job_id=:j",
                    o=ctx.org_id,
                    j=job_id,
                )
                if found:
                    r = found[0]
                    receipt = EmbedReceiptV1(
                        org_id=ctx.org_id,
                        job_id=r.job_id,
                        spec_id=r.spec_id,
                        chunk_set_id=r.chunk_set_id,
                        index_id=r.index_id,
                        chunk_count=r.chunk_count,
                        token_total=r.token_total,
                        coverage=r.coverage,
                        embedding_digest=r.embedding_digest,
                        ledger_digest=r.ledger_digest,
                    )
            return EmbedStatusV1(
                job=embed_job_record(row),
                receipt=receipt,
                can_cancel=row["state"] not in TERMINAL and row["state"] != "cancelling",
            )

    async def cancel(
        self, ctx: TenantContext, repository_id: UUID, snapshot_id: UUID, job_id: UUID
    ) -> EmbedStatusV1:
        async with transaction(self.factory) as session:
            row = await load_job(session, ctx.org_id, job_id)
        if (
            row["operation"] != "embed_chunk_set"
            or row["repository_id"] != repository_id
            or row["snapshot_id"] != snapshot_id
        ):
            raise ScopeViolation("embed job unavailable")
        await self.jobs.request_cancel(ctx, job_id)
        return await self.get(ctx, repository_id, row["snapshot_id"], job_id)


def _dump(value: Any) -> str:
    import json

    return json.dumps(value, sort_keys=True, separators=(",", ":"))
