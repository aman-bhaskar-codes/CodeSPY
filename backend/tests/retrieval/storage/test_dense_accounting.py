"""Job-bound embedding accounting (revision 0046): scope, replay, ledger and stage binding."""

import json
import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.retrieval.storage.test_dense_guards import (
    STAGE,
    direct_reservation,
    embed_case,
    params,
    reserve_payload,
    set_chunk_ids,
    set_worker,
    settled_reservation,
    vector,
    wrapper,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("CODESPY_TEST_SCHEMA_TARGET") != "head",
    reason="requires the current schema head (revision 0046)",
)

RESERVE = "SELECT public.codespy_embed_reserve(CAST(:p AS jsonb))"


async def case_and_chunks(api_factory, broker_factory, migration_factory, tmp_path):
    case = await embed_case(api_factory, broker_factory, migration_factory, tmp_path)
    return case, await set_chunk_ids(migration_factory, case)


async def try_reserve(broker_factory, case, payload, *, context=True):
    async with broker_factory() as session, session.begin():
        if context:
            await set_worker(session, case["ctx"].org_id, case["binding"])
        value = await session.scalar(text(RESERVE), {"p": json.dumps(payload)})
        return json.loads(value) if isinstance(value, str) else value


async def test_reserve_requires_a_live_embed_attempt_and_exact_scope(
    api_factory, broker_factory, migration_factory, tmp_path
):
    case, chunks = await case_and_chunks(api_factory, broker_factory, migration_factory, tmp_path)
    chunk = chunks[0]
    with pytest.raises(DBAPIError, match="worker context required"):
        await try_reserve(broker_factory, case, reserve_payload(case, chunk), context=False)
    for override in (
        {"provider": "other"},
        {"model_id": "other-model"},
        {"principal_id": str(uuid4())},
        {"org_id": str(uuid4())},
        {"repository_id": str(uuid4())},
    ):
        with pytest.raises(DBAPIError, match="embed_scope_denied"):
            await try_reserve(broker_factory, case, reserve_payload(case, chunk, **override))
    with pytest.raises(DBAPIError, match="embed_scope_denied"):  # a chunk outside the chunk set
        await try_reserve(broker_factory, case, reserve_payload(case, uuid4()))
    async with migration_factory() as session:
        assert await session.scalar(text("SELECT count(*) FROM embed_spend_ledger")) == 0


async def test_reserve_replays_one_reservation_per_chunk_and_refuses_a_changed_request(
    api_factory, broker_factory, migration_factory, tmp_path
):
    case, chunks = await case_and_chunks(api_factory, broker_factory, migration_factory, tmp_path)
    payload = reserve_payload(case, chunks[0])
    first = await try_reserve(broker_factory, case, payload)
    again = await try_reserve(broker_factory, case, payload)
    assert first["reservation_id"] == again["reservation_id"]
    assert first["principal_id"] == str(case["ctx"].principal_id)
    assert first["idempotency_key"] == f"embed:{case['spec_id']}:{chunks[0]}"
    with pytest.raises(DBAPIError, match="embedding_conflict"):
        await try_reserve(
            broker_factory, case, reserve_payload(case, chunks[0], estimated_tokens=7)
        )
    async with migration_factory() as session:
        assert await session.scalar(text("SELECT count(*) FROM embed_spend_ledger")) == 1


async def test_claim_and_settle_only_touch_reservations_in_this_jobs_ledger(
    api_factory, broker_factory, migration_factory, tmp_path
):
    case, chunks = await case_and_chunks(api_factory, broker_factory, migration_factory, tmp_path)
    foreign = await direct_reservation(case, api_factory, broker_factory, "outside-the-ledger")
    for statement, parameters in (
        ("SELECT public.codespy_embed_claim(CAST(:r AS uuid))", {"r": str(foreign)}),
        (
            "SELECT public.codespy_embed_settle(CAST(:r AS uuid), CAST(:p AS jsonb))",
            {"r": str(foreign), "p": json.dumps({"status": "failed"})},
        ),
    ):
        with pytest.raises(DBAPIError, match="embed_scope_denied"):
            async with broker_factory() as session, session.begin():
                await wrapper(session, case, statement, **parameters)
    reserved = await try_reserve(broker_factory, case, reserve_payload(case, chunks[0]))
    rid = reserved["reservation_id"]
    claim = "SELECT public.codespy_embed_claim(CAST(:r AS uuid))"
    with pytest.raises(DBAPIError, match="worker context required"):
        async with broker_factory() as session, session.begin():
            await session.scalar(text(claim), {"r": rid})
    outcomes = []
    for _ in range(2):
        async with broker_factory() as session, session.begin():
            outcomes.append((await wrapper(session, case, claim, r=rid))["claimed"])
    assert outcomes == [True, False]  # exactly one caller may dispatch


async def test_a_failure_after_the_claim_is_an_uncertain_hold_the_ledger_remembers(
    api_factory, broker_factory, migration_factory, tmp_path
):
    case, chunks = await case_and_chunks(api_factory, broker_factory, migration_factory, tmp_path)
    reserved = await try_reserve(broker_factory, case, reserve_payload(case, chunks[0]))
    rid = reserved["reservation_id"]
    async with broker_factory() as session, session.begin():
        await wrapper(session, case, "SELECT public.codespy_embed_claim(CAST(:r AS uuid))", r=rid)
    async with broker_factory() as session, session.begin():
        held = await wrapper(
            session,
            case,
            "SELECT public.codespy_embed_settle(CAST(:r AS uuid), CAST(:p AS jsonb))",
            r=rid,
            p=json.dumps({"status": "timeout"}),
        )
    assert held["state"] == "uncertain_hold"
    # a retry reserves the same chunk again and gets the same held reservation back
    again = await try_reserve(broker_factory, case, reserve_payload(case, chunks[0]))
    assert again["reservation_id"] == rid and again["state"] == "uncertain_hold"


async def test_ledger_is_immutable_and_staged_vectors_need_a_ledgered_settled_reservation(
    api_factory, broker_factory, migration_factory, tmp_path
):
    case, chunks = await case_and_chunks(api_factory, broker_factory, migration_factory, tmp_path)
    ledgered = await settled_reservation(case, broker_factory, chunks[0])
    for statement in (
        "UPDATE embed_spend_ledger SET fence=2 WHERE org_id=:org",
        "DELETE FROM embed_spend_ledger WHERE org_id=:org",
    ):
        with pytest.raises(DBAPIError):
            async with migration_factory() as session, session.begin():
                await session.execute(text(statement), params(case))
    outside = await direct_reservation(case, api_factory, broker_factory, "settled-but-unledgered")
    for chunk, reservation, message in (
        (chunks[0], outside, "no settled reservation"),  # not the chunk's ledgered reservation
        (chunks[-1] if len(chunks) > 1 else chunks[0], ledgered, "no settled reservation"),
    ):
        if chunk == chunks[0] and reservation == ledgered:
            continue
        with pytest.raises(DBAPIError, match=message):
            async with broker_factory() as session, session.begin():
                await set_worker(session, case["ctx"].org_id, case["binding"])
                await session.execute(
                    STAGE,
                    params(case, chunk=chunk, res=reservation, vec=json.dumps(vector())),
                )
    async with broker_factory() as session, session.begin():  # the ledgered one is accepted
        await set_worker(session, case["ctx"].org_id, case["binding"])
        await session.execute(
            STAGE, params(case, chunk=chunks[0], res=ledgered, vec=json.dumps(vector()))
        )
