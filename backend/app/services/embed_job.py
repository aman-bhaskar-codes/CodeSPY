# ruff: noqa: E501
"""Embed a sealed chunk set through accounted provider calls and publish a dense index.

Per chunk, under a live fenced attempt: reserve -> claim (committed before any I/O) -> embed ->
stage the vector durably -> settle. A crash therefore never loses a paid vector, and a retry
reuses staged work instead of paying twice. When every chunk is staged and settled, one fenced
transaction inserts the sealed dense index, the embeddings, the receipt and the terminal state.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.model.http_transport import ProviderTransportError
from app.contracts.embedding_index import EmbedConfigV1, EmbedReceiptV1, EmbedSpecV1
from app.contracts.jobs import AttemptLeaseV2
from app.contracts.provider_budget import (
    DisclosurePolicyV1,
    EmbeddingRequestV1,
    calculate_charge_micros,
)
from app.db.time import database_now
from app.db.transaction import transaction
from app.domain.errors import DuplicateConflict, FinalizationOutcomeUnknown, StaleAttempt
from app.ports.embedding import EmbeddingPort
from app.repositories.event_repository import append_event
from app.repositories.job_repository import load_job, worker_context
from app.services.accounted_embedding import estimated_tokens
from app.services.finalization_budget import FinalizationBudget, rollback_or_invalidate
from app.services.publication import PublicationExecutor

logger = logging.getLogger(__name__)
SAFETY_SECONDS = 90.0
EMBED_REGION = "ap-south-1"


class EmbedFailure(Exception):  # noqa: N818
    """A stable, payload-free failure code for the consumer to record."""

    def __init__(self, code: str, *, transient: bool) -> None:
        self.code, self.transient = code, transient
        super().__init__(code)


@dataclass(frozen=True)
class ChunkWork:
    chunk_id: UUID
    text: str


async def _fetch(session: AsyncSession, query: str, **params: Any) -> list[Any]:
    return list((await session.execute(text(query), params)).all())


def _json(value: Any) -> dict[str, Any]:
    loaded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(loaded, dict):
        raise EmbedFailure("embed_protocol_error", transient=False)
    return loaded


class EmbedOperation:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        embedder: EmbeddingPort,
        *,
        concurrency: int = 4,
        finalizer: "EmbedFinalizer | None" = None,
        policy_for: Callable[[UUID, UUID], DisclosurePolicyV1] | None = None,
    ) -> None:
        if not 1 <= concurrency <= 8:
            raise ValueError("concurrency must be between 1 and 8")
        self.factory, self.embedder, self.concurrency = factory, embedder, concurrency
        self.finalizer = finalizer or EmbedFinalizer(factory)
        # Without a resolver the default policy applies, which refuses every external provider.
        self.policy_for = policy_for or (lambda org, repository: DisclosurePolicyV1())

    # ---- inputs -----------------------------------------------------------------------------
    async def load(self, lease: AttemptLeaseV2) -> tuple[UUID, EmbedSpecV1, float]:
        async with transaction(self.factory) as session:
            await worker_context(session, lease)
            found = await _fetch(
                session,
                "SELECT operation,state,current_attempt_id,fence,deadline,embed_spec_id "
                "FROM jobs WHERE org_id=:o AND id=:j",
                o=lease.org_id,
                j=lease.job_id,
            )
            job = found[0] if found else None
            if (
                job is None
                or job.operation != "embed_chunk_set"
                or job.state != "running"
                or job.current_attempt_id != lease.attempt_id
                or job.fence != lease.fence
            ):
                raise StaleAttempt("embed attempt is not current")
            row = (
                await _fetch(
                    session,
                    "SELECT id,repository_id,snapshot_id,principal_id,chunk_set_id,provider,model_id,"
                    "dimension,normalize,price_version FROM embed_specs WHERE org_id=:o AND id=:s",
                    o=lease.org_id,
                    s=job.embed_spec_id,
                )
            )[0]
            spec = EmbedSpecV1(
                org_id=lease.org_id,
                repository_id=row.repository_id,
                snapshot_id=row.snapshot_id,
                principal_id=row.principal_id,
                chunk_set_id=row.chunk_set_id,
                config=EmbedConfigV1(
                    provider=row.provider,
                    model_id=row.model_id,
                    dimension=row.dimension,
                    normalize=row.normalize,
                    price_version=row.price_version,
                ).validated(),
            )
            seconds = (job.deadline - await database_now(session)).total_seconds()
        return row.id, spec, seconds - SAFETY_SECONDS

    async def chunks(self, lease: AttemptLeaseV2, spec: EmbedSpecV1) -> list[ChunkWork]:
        async with transaction(self.factory) as session:
            rows = await _fetch(
                session,
                "SELECT id,safe_content FROM source_chunks WHERE org_id=:o AND chunk_set_id=:s ORDER BY id",
                o=spec.org_id,
                s=spec.chunk_set_id,
            )
        return [ChunkWork(r.id, r.safe_content) for r in rows]

    async def price_micros(self, spec: EmbedSpecV1) -> int:
        async with transaction(self.factory) as session:
            rows = await _fetch(
                session,
                "SELECT input_micros_per_1k FROM price_schedules WHERE provider=:p AND model_id=:m "
                "AND version=:v AND effective_at<=clock_timestamp() AND expires_at>clock_timestamp()",
                p=spec.config.provider,
                m=spec.config.model_id,
                v=spec.config.price_version,
            )
        if not rows:
            raise EmbedFailure("embed_price_unavailable", transient=False)
        return int(rows[0].input_micros_per_1k)

    # ---- one chunk --------------------------------------------------------------------------
    async def _call(
        self,
        lease: AttemptLeaseV2,
        statement: str,
        **params: Any,
    ) -> Any:
        async with transaction(self.factory) as session:
            await worker_context(session, lease)
            return (await session.execute(text(statement), params)).scalar()

    async def embed_chunk(
        self,
        lease: AttemptLeaseV2,
        spec: EmbedSpecV1,
        spec_id: UUID,
        work: ChunkWork,
        rate: int,
    ) -> None:
        config = spec.config
        tokens = estimated_tokens(work.text)
        payload = {
            "org_id": str(spec.org_id),
            "repository_id": str(spec.repository_id),
            "principal_id": str(spec.principal_id),
            "provider": config.provider,
            "model_id": config.model_id,
            "estimated_tokens": tokens,
            "max_charge_micros": max(1, calculate_charge_micros(tokens, rate)),
            "chunk_id": str(work.chunk_id),
        }
        reserved = _json(
            await self._call(
                lease,
                "SELECT public.codespy_embed_reserve(CAST(:p AS jsonb))",
                p=json.dumps(payload),
            )
        )
        rid = str(reserved["reservation_id"])
        state = reserved["state"]
        staged = await self._staged(spec, work.chunk_id)
        if state == "committed":
            return
        if state not in ("reserved",) and staged is None:
            raise EmbedFailure("embed_hold_pending", transient=False)
        if staged is not None:  # paid earlier; only settlement can be missing
            await self._settle(lease, rid, "success", staged)
            return
        if reserved.get("dispatch_intent_at") is not None:
            # A previous attempt claimed this call and left no result: never send it again.
            await self._settle(lease, rid, "timeout", 0)
            raise EmbedFailure("embed_hold_pending", transient=False)
        claimed = _json(
            await self._call(lease, "SELECT public.codespy_embed_claim(CAST(:r AS uuid))", r=rid)
        )
        if claimed.get("claimed") is not True:
            raise EmbedFailure("embed_dispatch_conflict", transient=True)
        request = EmbeddingRequestV1(
            text=work.text,
            provider=config.provider,
            model_id=config.model_id,
            dimensions=config.dimension,
            normalize=config.normalize,
        )
        try:
            response = await self.embedder.embed(request)
        except asyncio.CancelledError:
            # Cancelled while the call may be in flight: hold the spend, then keep cancelling.
            await asyncio.shield(self._settle(lease, rid, "timeout", 0))
            raise
        except Exception as error:
            timed_out = isinstance(error, TimeoutError) or (
                isinstance(error, ProviderTransportError) and error.code == "provider_timeout"
            )
            await self._settle(lease, rid, "timeout" if timed_out else "failed", 0)
            raise EmbedFailure(
                "embed_provider_unavailable" if not timed_out else "embed_provider_timeout",
                transient=True,
            ) from None
        await self._call(
            lease,
            "INSERT INTO embed_vector_stage(org_id,spec_id,chunk_id,chunk_set_id,repository_id,"
            "snapshot_id,reservation_id,embedding_json,token_count,provider_request_id) "
            "VALUES(:o,:s,:c,:set,:repo,:snap,CAST(:r AS uuid),CAST(:v AS jsonb),:t,:req) RETURNING 1",
            o=spec.org_id,
            s=spec_id,
            c=work.chunk_id,
            set=spec.chunk_set_id,
            repo=spec.repository_id,
            snap=spec.snapshot_id,
            r=rid,
            v=json.dumps(list(response.vector)),
            t=response.token_count,
            req=response.provider_request_id,
        )
        await self._settle(lease, rid, "success", response.token_count)

    async def _staged(self, spec: EmbedSpecV1, chunk_id: UUID) -> int | None:
        async with transaction(self.factory) as session:
            rows = await _fetch(
                session,
                "SELECT token_count FROM embed_vector_stage WHERE org_id=:o AND chunk_id=:c "
                "AND spec_id=(SELECT id FROM embed_specs WHERE org_id=:o AND chunk_set_id=:set "
                "AND provider=:p AND model_id=:m AND dimension=:d AND normalize=:n AND price_version=:v)",
                o=spec.org_id,
                c=chunk_id,
                set=spec.chunk_set_id,
                p=spec.config.provider,
                m=spec.config.model_id,
                d=spec.config.dimension,
                n=spec.config.normalize,
                v=spec.config.price_version,
            )
        return int(rows[0].token_count) if rows else None

    async def _settle(self, lease: AttemptLeaseV2, rid: str, status: str, tokens: int) -> None:
        charge = 0
        if status == "success":
            rate = await self._call(
                lease,
                "SELECT captured_input_rate FROM embedding_reservations "
                "WHERE org_id=:o AND reservation_id=CAST(:r AS uuid)",
                o=lease.org_id,
                r=rid,
            )
            if rate is None:
                raise EmbedFailure("embed_protocol_error", transient=False)
            charge = calculate_charge_micros(tokens, int(rate))
        await self._call(
            lease,
            "SELECT public.codespy_embed_settle(CAST(:r AS uuid), CAST(:p AS jsonb))",
            r=rid,
            p=json.dumps(
                {"status": status, "actual_tokens": tokens, "actual_charge_micros": charge}
            ),
        )

    # ---- orchestration ----------------------------------------------------------------------
    async def execute(
        self,
        lease: AttemptLeaseV2,
        *,
        enter_finalization: Callable[[], Awaitable[None]] | None = None,
    ) -> EmbedReceiptV1:
        spec_id, spec, seconds = await self.load(lease)
        if seconds <= 0:
            raise EmbedFailure("embed_timeout", transient=True)
        policy = self.policy_for(spec.org_id, spec.repository_id)
        if not policy.is_provider_permitted(spec.config.provider) or not policy.is_region_permitted(
            EMBED_REGION
        ):
            raise EmbedFailure("embed_disclosure_denied", transient=False)
        rate = await self.price_micros(spec)
        work = await self.chunks(lease, spec)
        if not work:
            raise EmbedFailure("embed_empty_chunk_set", transient=False)
        gate = asyncio.Semaphore(self.concurrency)
        failures: list[EmbedFailure] = []

        async def one(item: ChunkWork) -> None:
            async with gate:
                if failures:
                    return
                try:
                    await self.embed_chunk(lease, spec, spec_id, item, rate)
                except EmbedFailure as failure:
                    failures.append(failure)

        try:
            async with asyncio.timeout(seconds):
                await asyncio.gather(*(one(item) for item in work))
        except TimeoutError:
            raise EmbedFailure("embed_timeout", transient=True) from None
        if failures:
            raise failures[0]
        if enter_finalization is not None:
            await enter_finalization()
        return await self.finalizer.finish(lease, spec, spec_id)


class EmbedFinalizer:
    """One fenced transaction: sealed dense index, embeddings, receipt, terminal job state."""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        budget_seconds: float = 60,
        safety_seconds: float = 5,
        cleanup_seconds: float = 5,
    ) -> None:
        self.factory, self.cleanup_seconds = factory, cleanup_seconds
        self.executor = PublicationExecutor(
            factory,
            budget_seconds=budget_seconds,
            safety_seconds=safety_seconds,
            cleanup_seconds=cleanup_seconds,
        )

    async def _stored(self, session: AsyncSession, lease: AttemptLeaseV2) -> EmbedReceiptV1:
        rows = await _fetch(
            session,
            "SELECT p.job_id,p.spec_id,p.chunk_set_id,p.index_id,p.chunk_count,p.token_total,"
            "p.coverage,p.embedding_digest,p.ledger_digest,j.state,j.fence,j.current_attempt_id "
            "FROM embed_publication_receipts p JOIN jobs j ON j.org_id=p.org_id AND j.id=p.job_id "
            "WHERE p.org_id=:o AND p.job_id=:j AND p.attempt_id=:a AND p.fence=:f",
            o=lease.org_id,
            j=lease.job_id,
            a=lease.attempt_id,
            f=lease.fence,
        )
        if not rows or rows[0].state not in ("succeeded", "partial"):
            raise StaleAttempt("dense publication has no durable terminal receipt")
        r = rows[0]
        return EmbedReceiptV1(
            org_id=lease.org_id,
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

    async def _durable(self, lease: AttemptLeaseV2) -> EmbedReceiptV1:
        session = self.factory()
        try:
            async with asyncio.timeout(5), session.begin():
                await session.execute(text("SET LOCAL statement_timeout='2s'"))
                return await self._stored(session, lease)
        except StaleAttempt:
            raise
        except Exception:
            raise FinalizationOutcomeUnknown(
                "dense publication outcome requires recovery"
            ) from None
        finally:
            await rollback_or_invalidate(session, self.cleanup_seconds)

    async def finish(
        self, lease: AttemptLeaseV2, spec: EmbedSpecV1, spec_id: UUID
    ) -> EmbedReceiptV1:
        index_id = spec.dense_index_id()
        c = spec.config

        async def work(session: AsyncSession, budget: FinalizationBudget) -> EmbedReceiptV1:
            await worker_context(session, lease)
            hint = await load_job(session, lease.org_id, lease.job_id)
            if hint["state"] in ("succeeded", "partial"):
                return await self._stored(session, lease)
            sets = await _fetch(
                session,
                "SELECT coverage,chunk_count FROM retrieval_chunk_sets WHERE org_id=:o AND id=:s",
                o=spec.org_id,
                s=spec.chunk_set_id,
            )
            if not sets:
                raise DuplicateConflict("dense publication chunk set differs")
            coverage, count = sets[0].coverage, sets[0].chunk_count
            params = {
                "o": spec.org_id,
                "spec": spec_id,
                "repo": spec.repository_id,
                "snap": spec.snapshot_id,
                "set": spec.chunk_set_id,
                "idx": index_id,
                "prov": c.provider,
                "model": c.model_id,
                "dim": c.dimension,
                "n": count,
                "state": "ready" if coverage == "complete" else "partial_ready",
                "job": lease.job_id,
                "att": lease.attempt_id,
                "fence": lease.fence,
                "cov": coverage,
            }
            for statement in (
                "INSERT INTO search_indices(org_id,id,repository_id,snapshot_id,index_type,state,"
                "embedding_provider,embedding_model,dimension,chunk_set_id,total_chunks,"
                "indexed_chunks,failed_chunks,sealed_at) VALUES(:o,:idx,:repo,:snap,'dense',:state,"
                ":prov,:model,:dim,:set,:n,:n,0,clock_timestamp())",
                "INSERT INTO chunk_embeddings(org_id,chunk_id,index_id,repository_id,snapshot_id,"
                "chunk_set_id,embedding_json,token_count) SELECT org_id,chunk_id,:idx,repository_id,"
                "snapshot_id,chunk_set_id,embedding_json,token_count FROM embed_vector_stage "
                "WHERE org_id=:o AND spec_id=:spec ORDER BY chunk_id",
                "INSERT INTO embed_publication_receipts(org_id,job_id,attempt_id,fence,repository_id,"
                "snapshot_id,spec_id,spec_hash,chunk_set_id,index_id,chunk_count,token_total,"
                "embedding_digest,ledger_digest,coverage) SELECT :o,:job,:att,:fence,:repo,:snap,s.id,"
                "s.spec_hash,:set,:idx,(SELECT count(*) FROM chunk_embeddings WHERE org_id=:o "
                "AND index_id=:idx),(SELECT coalesce(sum(r.actual_tokens),0) FROM embed_spend_ledger l "
                "JOIN embedding_reservations r ON r.org_id=l.org_id AND r.reservation_id=l.reservation_id "
                "WHERE l.org_id=:o AND l.spec_id=:spec AND r.state='committed'),"
                "public.codespy_embed_embedding_digest(:o,:idx),"
                "public.codespy_embed_ledger_digest(:o,s.id),:cov FROM embed_specs s "
                "WHERE s.org_id=:o AND s.id=:spec",
                "DELETE FROM embed_vector_stage WHERE org_id=:o AND spec_id=:spec",
            ):
                if budget.remaining() <= 0:
                    raise TimeoutError("dense finalization deadline exceeded")
                await session.execute(text(statement), params)
            updated = (
                (
                    await session.execute(
                        text(
                            "UPDATE jobs SET state=CASE WHEN :cov='complete' THEN 'succeeded' "
                            "ELSE 'partial' END,result_index_id=:idx,finished_at=clock_timestamp() "
                            "WHERE org_id=:o AND id=:job RETURNING *"
                        ),
                        params,
                    )
                )
                .mappings()
                .one()
            )
            await session.execute(
                text(
                    "UPDATE job_attempts SET state='succeeded',finished_at=clock_timestamp() "
                    "WHERE org_id=:o AND job_id=:job AND id=:att"
                ),
                params,
            )
            await append_event(
                session,
                dict(updated),
                "embed_completed" if coverage == "complete" else "embed_partial",
                {"stage": "publication"},
            )
            return await self._stored(session, lease)

        return await self.executor.execute(work, lambda: self._durable(lease))
