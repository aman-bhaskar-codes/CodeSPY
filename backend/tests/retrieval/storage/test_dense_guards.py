# ruff: noqa: E501
"""Dense publication guards (revision 0045): who may write, what must match, what stays sealed."""

import json
import os
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4, uuid5

import pytest
from app.contracts.provider_budget import (
    BudgetReservationRequestV1,
    DisclosurePolicyV1,
    PriceScheduleV1,
    SettlementOutcomeV1,
)
from app.models.phase2_tables import PHASE2_TABLES
from app.services.provider_budget import ProviderBudgetService
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.retrieval.storage.test_dense_schema import published_lexical
from tests.retrieval.storage.test_index_guards import owned_attempt, set_worker

pytestmark = pytest.mark.skipif(
    os.environ.get("CODESPY_TEST_SCHEMA_TARGET") != "head",
    reason="requires the current schema head (revision 0045)",
)

PROVIDER, MODEL, DIM = "aws_bedrock", "amazon.titan-embed-text-v2:0", 256
POLICY = DisclosurePolicyV1(allow_external_disclosure=True, allowed_providers=(PROVIDER,))


def vector(seed: float = 1.0):
    values = [round(seed * (i + 1) / 1000, 6) for i in range(DIM)]
    return values


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


async def embed_case(api_factory, broker_factory, migration_factory, tmp_path):
    """A published lexical set, a funded budget, an embed spec and a running embed attempt."""
    ctx, spec, _, receipt = await published_lexical(
        api_factory, broker_factory, migration_factory, tmp_path
    )
    set_id, lexical_id = receipt["chunk_set_id"], receipt["index_id"]
    config = {"provider": PROVIDER, "model_id": MODEL, "dimension": DIM, "normalize": True}
    spec_id = uuid4()
    spec_json = {
        "org_id": str(ctx.org_id),
        "repository_id": str(spec.repository_id),
        "snapshot_id": str(spec.snapshot_id),
        "principal_id": str(ctx.principal_id),
        "chunk_set_id": str(set_id),
        "config": config,
    }
    text_value = canonical(spec_json)
    spec_hash = sha256(text_value.encode()).hexdigest()
    async with migration_factory() as session, session.begin():
        service = ProviderBudgetService(session)
        await service.ensure_budget(ctx.org_id, spec.repository_id, 1_000_000)
        now = datetime.now(UTC)
        await service.set_price_schedule(
            PriceScheduleV1(
                provider=PROVIDER,
                model_id=MODEL,
                version="test",
                input_micros_per_1k=1000,
                effective_at=now - timedelta(days=1),
                expires_at=now + timedelta(days=1),
            )
        )
        config_hash = await session.scalar(
            text("SELECT public.codespy_scan_digest(CAST(:c AS jsonb))"), {"c": canonical(config)}
        )
    async with api_factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO embed_specs(org_id,id,repository_id,snapshot_id,principal_id,chunk_set_id,"
                "lexical_index_id,provider,model_id,dimension,normalize,price_version,config_hash,"
                "spec_hash,config_json,spec_json,canonical_text) VALUES(:org,:id,:repo,:snap,:pr,:set,"
                ":lex,:prov,:model,:dim,true,'test',:ch,:sh,CAST(:cj AS jsonb),CAST(:sj AS jsonb),:ct)"
            ),
            {
                "org": ctx.org_id,
                "id": spec_id,
                "repo": spec.repository_id,
                "snap": spec.snapshot_id,
                "pr": ctx.principal_id,
                "set": set_id,
                "lex": lexical_id,
                "prov": PROVIDER,
                "model": MODEL,
                "dim": DIM,
                "ch": config_hash,
                "sh": spec_hash,
                "cj": canonical(config),
                "sj": text_value,
                "ct": text_value,
            },
        )
    job_id, attempt_id = uuid4(), uuid4()
    async with migration_factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO jobs(org_id,id,repository_id,snapshot_id,principal_id,embed_spec_id,"
                "config_hash,request_key,request_hash,operation,deadline) VALUES(:org,:id,:repo,:snap,"
                ":pr,:spec,:ch,:key,:rh,'embed_chunk_set',clock_timestamp()+interval '15 minutes')"
            ),
            {
                "org": ctx.org_id,
                "id": job_id,
                "repo": spec.repository_id,
                "snap": spec.snapshot_id,
                "pr": ctx.principal_id,
                "spec": spec_id,
                "ch": config_hash,
                "key": str(job_id),
                "rh": "1" * 64,
            },
        )
        attempts = PHASE2_TABLES["job_attempts"]
        await session.execute(
            attempts.insert().values(
                org_id=ctx.org_id,
                job_id=job_id,
                id=attempt_id,
                fence=1,
                owner="embed-fixture",
                lease_expires_at=text("clock_timestamp()+interval '90 seconds'"),
                heartbeat_at=text("clock_timestamp()"),
            )
        )
        await session.execute(
            text(
                "UPDATE jobs SET state='running',attempt_count=1,fence=1,current_attempt_id=:a "
                "WHERE org_id=:org AND id=:id"
            ),
            {"a": attempt_id, "org": ctx.org_id, "id": job_id},
        )
        binding = (job_id, attempt_id, 1)
        await set_worker(session, ctx.org_id, binding)
        await session.execute(
            text(
                "INSERT INTO embed_attempt_specs(org_id,id,job_id,fence,spec_id,repository_id,"
                "snapshot_id,spec_hash) VALUES(:org,:a,:j,1,:s,:repo,:snap,:sh)"
            ),
            {
                "org": ctx.org_id,
                "a": attempt_id,
                "j": job_id,
                "s": spec_id,
                "repo": spec.repository_id,
                "snap": spec.snapshot_id,
                "sh": spec_hash,
            },
        )
    return {
        "ctx": ctx,
        "spec": spec,
        "spec_id": spec_id,
        "set_id": set_id,
        "binding": binding,
        "index_id": uuid5(set_id, f"dense-v1:{PROVIDER}:{MODEL}:{DIM}"),
        "chunks": [],
    }


def reserve_payload(case, chunk_id, **overrides):
    ctx, spec = case["ctx"], case["spec"]
    return {
        "org_id": str(ctx.org_id),
        "repository_id": str(spec.repository_id),
        "principal_id": str(ctx.principal_id),
        "provider": PROVIDER,
        "model_id": MODEL,
        "estimated_tokens": 100,
        "max_charge_micros": 1000,
        "chunk_id": str(chunk_id),
        **overrides,
    }


async def wrapper(session, case, statement, **parameters):
    await set_worker(session, case["ctx"].org_id, case["binding"])
    value = await session.scalar(text(statement), parameters)
    return json.loads(value) if isinstance(value, str) else value


async def settled_reservation(case, broker_factory, chunk_id):
    """Spend exactly as the embed worker does: job-bound reserve, claim, settle."""
    async with broker_factory() as session, session.begin():
        reserved = await wrapper(
            session,
            case,
            "SELECT public.codespy_embed_reserve(CAST(:p AS jsonb))",
            p=json.dumps(reserve_payload(case, chunk_id)),
        )
    reservation = reserved["reservation_id"]
    async with broker_factory() as session, session.begin():
        claimed = await wrapper(
            session, case, "SELECT public.codespy_embed_claim(CAST(:r AS uuid))", r=reservation
        )
        assert claimed["claimed"] is True
    async with broker_factory() as session, session.begin():
        await wrapper(
            session,
            case,
            "SELECT public.codespy_embed_settle(CAST(:r AS uuid), CAST(:p AS jsonb))",
            r=reservation,
            p=json.dumps({"status": "success", "actual_tokens": 10, "actual_charge_micros": 10}),
        )
    return UUID(reservation)


async def direct_reservation(case, api_factory, broker_factory, key):
    """A settled reservation that bypasses the ledger (what the old accounting path produced)."""
    ctx, spec = case["ctx"], case["spec"]
    request = BudgetReservationRequestV1(
        org_id=ctx.org_id,
        repository_id=spec.repository_id,
        principal_id=ctx.principal_id,
        provider=PROVIDER,
        model_id=MODEL,
        estimated_tokens=100,
        max_charge_micros=1000,
        idempotency_key=key,
    )
    async with api_factory() as session, session.begin():
        reservation = await ProviderBudgetService(session).reserve(request, POLICY)
    async with broker_factory() as session, session.begin():
        service = ProviderBudgetService(session)
        assert await service.mark_dispatched(ctx.org_id, reservation.reservation_id)
        await service.settle(
            ctx.org_id,
            SettlementOutcomeV1(
                reservation_id=reservation.reservation_id,
                status="success",
                actual_tokens=10,
                actual_charge_micros=10,
            ),
        )
    return reservation.reservation_id


async def set_chunk_ids(migration_factory, case):
    async with migration_factory() as session:
        rows = await session.execute(
            text("SELECT id FROM source_chunks WHERE org_id=:o AND chunk_set_id=:s ORDER BY id"),
            {"o": case["ctx"].org_id, "s": case["set_id"]},
        )
        return [row.id for row in rows]


STAGE = text(
    "INSERT INTO embed_vector_stage(org_id,spec_id,chunk_id,chunk_set_id,repository_id,snapshot_id,"
    "reservation_id,embedding_json,token_count) VALUES(:org,:spec,:chunk,:set,:repo,:snap,:res,"
    "CAST(:vec AS jsonb),10)"
)
INDEX = text(
    "INSERT INTO search_indices(org_id,id,repository_id,snapshot_id,index_type,state,embedding_provider,"
    "embedding_model,dimension,chunk_set_id,total_chunks,indexed_chunks,failed_chunks,sealed_at) "
    "VALUES(:org,:idx,:repo,:snap,'dense','ready',:prov,:model,:dim,:set,:n,:n,0,clock_timestamp())"
)
EMBED = text(
    "INSERT INTO chunk_embeddings(org_id,chunk_id,index_id,repository_id,snapshot_id,chunk_set_id,"
    "embedding_json,token_count) VALUES(:org,:chunk,:idx,:repo,:snap,:set,CAST(:vec AS jsonb),10)"
)
RECEIPT = text(
    "INSERT INTO embed_publication_receipts(org_id,job_id,attempt_id,fence,repository_id,snapshot_id,"
    "spec_id,spec_hash,chunk_set_id,index_id,chunk_count,token_total,embedding_digest,ledger_digest,"
    "coverage) SELECT :org,:job,:att,1,:repo,:snap,s.id,s.spec_hash,:set,:idx,:n,:tok,"
    "public.codespy_embed_embedding_digest(:org,:idx),public.codespy_embed_ledger_digest(:org,s.id),'complete' FROM embed_specs s "
    "WHERE s.org_id=:org AND s.id=:spec"
)


BAD_RECEIPT = text(
    RECEIPT.text.replace(
        "public.codespy_embed_embedding_digest(:org,:idx)", "repeat('3',64)"
    ).replace(":ledger", "repeat('2',64)")
)


def params(case, **extra):
    ctx, spec = case["ctx"], case["spec"]
    return {
        "org": ctx.org_id,
        "spec": case["spec_id"],
        "set": case["set_id"],
        "repo": spec.repository_id,
        "snap": spec.snapshot_id,
        "idx": case["index_id"],
        "prov": PROVIDER,
        "model": MODEL,
        "dim": DIM,
        "job": case["binding"][0],
        "att": case["binding"][1],
        **extra,
    }


async def publish(
    session,
    case,
    chunk_ids,
    reservations,
    *,
    vectors=None,
    terminal=True,
    cleanup=False,
    receipt=True,
):
    vectors = vectors or {c: vector(i + 1) for i, c in enumerate(chunk_ids)}
    for chunk, reservation in zip(chunk_ids, reservations, strict=True):
        await session.execute(
            STAGE,
            params(case, chunk=chunk, res=reservation, vec=json.dumps(vectors[chunk])),
        )
    await session.execute(INDEX, params(case, n=len(chunk_ids)))
    for chunk in chunk_ids:
        await session.execute(EMBED, params(case, chunk=chunk, vec=json.dumps(vectors[chunk])))
    if receipt:
        await session.execute(RECEIPT, params(case, n=len(chunk_ids), tok=10 * len(chunk_ids)))
    if cleanup:  # staged vectors are removed while the attempt is still live
        await session.execute(
            text("DELETE FROM embed_vector_stage WHERE org_id=:org AND spec_id=:spec"),
            params(case),
        )
    if terminal:
        await session.execute(
            text(
                "UPDATE jobs SET state='succeeded',result_index_id=:idx,finished_at=clock_timestamp() "
                "WHERE org_id=:org AND id=:job"
            ),
            params(case),
        )
        await session.execute(
            text(
                "UPDATE job_attempts SET state='succeeded',finished_at=clock_timestamp() "
                "WHERE org_id=:org AND id=:att"
            ),
            params(case),
        )


async def case_with_reservations(api_factory, broker_factory, migration_factory, tmp_path):
    case = await embed_case(api_factory, broker_factory, migration_factory, tmp_path)
    chunks = await set_chunk_ids(migration_factory, case)
    reservations = [await settled_reservation(case, broker_factory, chunk) for chunk in chunks]
    return case, chunks, reservations


async def test_fenced_embed_attempt_publishes_a_sealed_dense_index(
    api_factory, broker_factory, migration_factory, tmp_path
):
    case, chunks, reservations = await case_with_reservations(
        api_factory, broker_factory, migration_factory, tmp_path
    )
    assert chunks
    async with broker_factory() as session, session.begin():
        await set_worker(session, case["ctx"].org_id, case["binding"])
        await publish(session, case, chunks, reservations, cleanup=True)
    async with migration_factory() as session:
        assert await session.scalar(
            text("SELECT count(*) FROM chunk_embeddings WHERE index_id=:idx"), params(case)
        ) == len(chunks)
        assert (
            await session.scalar(
                text(
                    "SELECT embedding IS NOT NULL FROM chunk_embeddings WHERE index_id=:idx LIMIT 1"
                ),
                params(case),
            )
            is True
        )


EXPECTED = {
    "no_worker_context": "worker context required",
    "unsettled_reservation": "no settled reservation",
    "vector_differs_from_stage": "qualified embedding publication unavailable",
    "wrong_dimension": "embed staged vector differs",
    "index_wrong_id": "ck_index_qualified",
    "receipt_digest_wrong": "embed receipt differs",
    "missing_receipt_at_commit": "uncommitted dense",
}


@pytest.mark.parametrize(
    "scenario",
    [
        "no_worker_context",
        "unsettled_reservation",
        "vector_differs_from_stage",
        "wrong_dimension",
        "index_wrong_id",
        "receipt_digest_wrong",
        "missing_receipt_at_commit",
    ],
)
async def test_every_deviation_is_rejected_atomically(
    api_factory, broker_factory, migration_factory, tmp_path, scenario
):
    case, chunks, reservations = await case_with_reservations(
        api_factory, broker_factory, migration_factory, tmp_path
    )
    org = case["ctx"].org_id
    vectors = {c: vector(i + 1) for i, c in enumerate(chunks)}
    with pytest.raises(DBAPIError, match=EXPECTED[scenario]):
        async with broker_factory() as session, session.begin():
            if scenario != "no_worker_context":
                await set_worker(session, org, case["binding"])
            if scenario == "unsettled_reservation":
                reservations = [uuid4() for _ in chunks]
            if scenario == "wrong_dimension":
                vectors = {c: vector()[:128] for c in chunks}
            if scenario == "vector_differs_from_stage":
                for chunk, reservation in zip(chunks, reservations, strict=True):
                    await session.execute(
                        STAGE,
                        params(case, chunk=chunk, res=reservation, vec=json.dumps(vectors[chunk])),
                    )
                await session.execute(INDEX, params(case, n=len(chunks)))
                await session.execute(
                    EMBED, params(case, chunk=chunks[0], vec=json.dumps(vector(9.0)))
                )
            elif scenario == "index_wrong_id":
                await session.execute(
                    STAGE,
                    params(
                        case,
                        chunk=chunks[0],
                        res=reservations[0],
                        vec=json.dumps(vectors[chunks[0]]),
                    ),
                )
                await session.execute(INDEX, {**params(case, n=1), "idx": uuid4()})
            elif scenario == "receipt_digest_wrong":
                await publish(session, case, chunks, reservations, terminal=False, receipt=False)
                await session.execute(
                    BAD_RECEIPT, params(case, n=len(chunks), tok=10 * len(chunks))
                )
            elif scenario == "missing_receipt_at_commit":
                for chunk, reservation in zip(chunks, reservations, strict=True):
                    await session.execute(
                        STAGE,
                        params(case, chunk=chunk, res=reservation, vec=json.dumps(vectors[chunk])),
                    )
                await session.execute(INDEX, params(case, n=len(chunks)))
            else:
                await publish(session, case, chunks, reservations, vectors=vectors)
    async with migration_factory() as session:
        for query in (
            "SELECT count(*) FROM chunk_embeddings WHERE org_id=:org",
            "SELECT count(*) FROM embed_publication_receipts WHERE org_id=:org",
            "SELECT count(*) FROM embed_vector_stage WHERE org_id=:org",
        ):
            assert await session.scalar(text(query), params(case)) == 0
        assert (
            await session.scalar(
                text("SELECT count(*) FROM search_indices WHERE index_type='dense'")
            )
            == 0
        )


async def test_published_dense_rows_are_immutable_and_sealed(
    api_factory, broker_factory, migration_factory, tmp_path
):
    case, chunks, reservations = await case_with_reservations(
        api_factory, broker_factory, migration_factory, tmp_path
    )
    async with broker_factory() as session, session.begin():
        await set_worker(session, case["ctx"].org_id, case["binding"])
        await publish(session, case, chunks, reservations)
    for statement in (
        "UPDATE chunk_embeddings SET token_count=1 WHERE index_id=:idx",
        "DELETE FROM chunk_embeddings WHERE index_id=:idx",
        "UPDATE search_indices SET failed_chunks=1 WHERE id=:idx",
        "DELETE FROM search_indices WHERE id=:idx",
        "DELETE FROM embed_publication_receipts WHERE index_id=:idx",
        "UPDATE embed_vector_stage SET token_count=1 WHERE spec_id=:spec",
    ):
        with pytest.raises(DBAPIError):
            async with broker_factory() as session, session.begin():
                await set_worker(session, case["ctx"].org_id, case["binding"])
                await session.execute(text(statement), params(case))


async def test_other_operations_and_roles_cannot_publish_dense_rows(
    api_factory, broker_factory, migration_factory, tmp_path
):
    case, _chunks, _reservations = await case_with_reservations(
        api_factory, broker_factory, migration_factory, tmp_path
    )
    # an index_snapshot attempt on the same snapshot is not an embed attempt
    index_binding = await owned_attempt(
        migration_factory, case["ctx"], case["spec"], await index_spec_id(migration_factory, case)
    )
    with pytest.raises(DBAPIError):
        async with broker_factory() as session, session.begin():
            await set_worker(session, case["ctx"].org_id, index_binding)
            await session.execute(INDEX, params(case, n=1))
    # the API role has no write access to the worker-only tables
    with pytest.raises(DBAPIError):
        async with api_factory() as session, session.begin():
            await session.execute(INDEX, params(case, n=1))


async def index_spec_id(migration_factory, case):
    from app.models.index_tables import index_specs
    from sqlalchemy import select

    async with migration_factory() as session:
        return await session.scalar(
            select(index_specs.c.id).where(index_specs.c.org_id == case["ctx"].org_id)
        )
