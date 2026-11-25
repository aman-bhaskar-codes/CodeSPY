# ruff: noqa: E501
"""Event log storage on PostgreSQL: gap-free sequencing, guards, replay at every seq, live fan-out."""

import asyncio
import os
from uuid import UUID, uuid4

import pytest
from app.contracts.agent_events import validate_payload
from app.contracts.agent_session import AgentSessionCreateRequestV1
from app.domain.errors import DuplicateConflict, ScopeViolation
from app.services.agent.event_log import (
    AgentEventLog,
    EventCipher,
    LocalWaiter,
    PgNotifyWaiter,
    asyncpg_dsn,
    digest_events,
    tail,
)
from app.services.agent.sessions import AgentSessionService
from cryptography.exceptions import InvalidTag
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.agent.protocol.samples import RECORDING, SAMPLES
from tests.ast_facts.test_ast_facts_publication import admitted

pytestmark = pytest.mark.skipif(
    os.environ.get("CODESPY_TEST_SCHEMA_TARGET") != "head",
    reason="requires the current schema head (revision 0050)",
)

KEY = bytes(range(32))


async def new_session(factory, tmp_path, key="k1"):
    ctx, snapshot, repo, _store = await admitted(factory, tmp_path)
    log = AgentEventLog(EventCipher(KEY))
    service = AgentSessionService(factory, log)
    created = await service.create(
        ctx, AgentSessionCreateRequestV1(repository_id=repo, snapshot_id=snapshot), key
    )
    return ctx, log, service, created


async def record(factory, log, ctx, session_id, count=None):
    for kind, turn, parent in RECORDING[1:count]:  # session.created already exists
        async with factory() as s, s.begin():
            await log.append(
                s, ctx.org_id, session_id, kind, SAMPLES[kind], turn=turn, parent=parent
            )


async def frames(gen, n):
    out = []
    try:
        async with asyncio.timeout(10):
            async for frame in gen:
                out.append(frame)
                if len(out) == n:
                    break
    finally:
        await gen.aclose()
    return out


def seq_of(frame):
    return int(frame.split("\n", 1)[0].removeprefix("id: ")) if frame.startswith("id:") else 0


async def test_create_is_idempotent_and_writes_the_first_event(factory, tmp_path):
    ctx, _log, service, created = await new_session(factory, tmp_path)
    assert created.last_seq == 1 and created.status == "idle" and created.mode == "plan"
    same = await service.create(
        ctx,
        AgentSessionCreateRequestV1(
            repository_id=created.repository_id, snapshot_id=created.snapshot_id
        ),
        "k1",
    )
    assert same.id == created.id and same.last_seq == 1
    with pytest.raises(DuplicateConflict):
        await service.create(
            ctx,
            AgentSessionCreateRequestV1(
                repository_id=created.repository_id, snapshot_id=created.snapshot_id, mode="act"
            ),
            "k1",
        )
    state = await service.state(ctx, created.id)
    assert state.as_of_seq == 1 and state.mode == "plan"


async def test_a_session_is_visible_only_to_its_principal(factory, tmp_path):
    ctx, _log, service, created = await new_session(factory, tmp_path)
    other = ctx.model_copy(update={"principal_id": uuid4()})
    for call in (service.get(other, created.id), service.state(other, created.id)):
        with pytest.raises(ScopeViolation):
            await call
    with pytest.raises(ScopeViolation):
        await service.get(ctx.model_copy(update={"org_id": uuid4()}), created.id)


async def test_event_key_makes_append_idempotent_and_conflicts_are_rejected(factory, tmp_path):
    ctx, log, _service, created = await new_session(factory, tmp_path)
    payload = {"status": "running"}
    async with factory() as s, s.begin():
        first = await log.append(
            s, ctx.org_id, created.id, "session.status", payload, event_key="r1"
        )
    async with factory() as s, s.begin():
        again = await log.append(
            s, ctx.org_id, created.id, "session.status", payload, event_key="r1"
        )
    assert again == first and first.seq == 2
    with pytest.raises(DuplicateConflict):
        async with factory() as s, s.begin():
            await log.append(
                s, ctx.org_id, created.id, "session.status", {"status": "failed"}, event_key="r1"
            )
    async with factory() as s, s.begin():
        assert await log.head(s, ctx.org_id, created.id) == 2


async def test_append_rejects_invalid_unknown_transient_and_foreign_sessions(factory, tmp_path):
    ctx, log, _service, created = await new_session(factory, tmp_path)
    async with factory() as s, s.begin():
        with pytest.raises(ValidationError):
            await log.append(s, ctx.org_id, created.id, "session.status", {"status": "bogus"})
        with pytest.raises(ValueError, match="unknown event type"):
            await log.append(s, ctx.org_id, created.id, "made.up", {})
        with pytest.raises(ValueError, match="transient"):
            await log.append(s, ctx.org_id, created.id, "stream.reset", {"reason": "replay_limit"})
        with pytest.raises(ScopeViolation):
            await log.append(s, uuid4(), created.id, "session.status", {"status": "idle"})


async def test_the_payload_is_encrypted_at_rest_and_bound_to_its_row(factory, tmp_path):
    ctx, log, _service, created = await new_session(factory, tmp_path)
    async with factory() as s, s.begin():
        for chunk in ("AA", "BB"):
            await log.append(
                s,
                ctx.org_id,
                created.id,
                "message.delta",
                {"message_id": "m", "offset": 0, "text": chunk},
                turn=1,
            )
    async with factory() as s, s.begin():
        raw = (
            await s.execute(
                text(
                    "SELECT payload_ciphertext, payload_bytes FROM agent_events WHERE session_id=:i AND seq=2"
                ),
                {"i": created.id},
            )
        ).one()
        assert b"AA" not in bytes(raw[0]) and len(bytes(raw[0])) == raw[1] + 28
    # Move seq 2's ciphertext onto seq 3 by the owner role: the row no longer decrypts.
    async with factory() as s, s.begin():
        await s.execute(text("ALTER TABLE agent_events DISABLE TRIGGER USER"))
        await s.execute(
            text(
                "UPDATE agent_events SET payload_ciphertext=(SELECT payload_ciphertext FROM agent_events"
                " WHERE session_id=:i AND seq=2) WHERE session_id=:i AND seq=3"
            ),
            {"i": created.id},
        )
        await s.execute(text("ALTER TABLE agent_events ENABLE TRIGGER USER"))
    async with factory() as s, s.begin():
        with pytest.raises((InvalidTag, ValidationError, ValueError)):
            await log.read_after(s, ctx.org_id, created.id, 2)


async def test_one_thousand_concurrent_writers_produce_a_gap_free_sequence(factory, tmp_path):
    ctx, log, _service, created = await new_session(factory, tmp_path)

    async def write(i):
        async with factory() as s, s.begin():
            return await log.append(
                s,
                ctx.org_id,
                created.id,
                "message.delta",
                {"message_id": "m", "offset": i, "text": "x"},
                turn=1,
            )

    stored = await asyncio.gather(*(write(i) for i in range(1000)))
    assert sorted(e.seq for e in stored) == list(range(2, 1002))
    async with factory() as s, s.begin():
        rows = (
            (
                await s.execute(
                    text("SELECT seq FROM agent_events WHERE session_id=:i ORDER BY seq"),
                    {"i": created.id},
                )
            )
            .scalars()
            .all()
        )
        assert rows == list(range(1, 1002))
        assert await log.head(s, ctx.org_id, created.id) == 1001
        everything = await log.read_after(s, ctx.org_id, created.id, 0, limit=2000)
    assert [e.seq for e in everything] == list(range(1, 1002))
    assert digest_events(everything)  # decrypts every row


async def test_database_guards_make_the_sequence_a_fact_not_a_convention(factory, tmp_path):
    ctx, log, _service, created = await new_session(factory, tmp_path)
    sid = created.id
    insert = (
        "INSERT INTO agent_events(org_id,session_id,seq,type,payload_ciphertext,payload_bytes) "
        "VALUES(:o,:i,:q,:t,decode(repeat('00',30),'hex'),2)"
    )

    async def attempt(*statements):
        async with factory() as s, s.begin():
            for sql, params in statements:
                await s.execute(text(sql), params)

    base = {"o": ctx.org_id, "i": sid}
    with pytest.raises(
        DBAPIError, match="sequence must equal"
    ):  # skipping ahead without allocating
        await attempt((insert, {**base, "q": 5, "t": "session.status"}))
    with pytest.raises(DBAPIError):  # duplicate seq
        await attempt((insert, {**base, "q": 1, "t": "session.status"}))
    with pytest.raises(DBAPIError, match="advances by one"):  # jumping two
        await attempt(("UPDATE agent_sessions SET last_seq=last_seq+2 WHERE id=:i", {"i": sid}))
    with pytest.raises(DBAPIError, match="advances by one"):  # moving backwards
        await attempt(("UPDATE agent_sessions SET last_seq=0 WHERE id=:i", {"i": sid}))
    with pytest.raises(DBAPIError, match="has a gap"):  # allocating without writing the event
        await attempt(("UPDATE agent_sessions SET last_seq=last_seq+1 WHERE id=:i", {"i": sid}))
    with pytest.raises(DBAPIError, match="immutable"):  # events never change or disappear
        await attempt(("UPDATE agent_events SET turn=9 WHERE session_id=:i", {"i": sid}))
    with pytest.raises(DBAPIError, match="immutable"):
        await attempt(("DELETE FROM agent_events WHERE session_id=:i", {"i": sid}))
    with pytest.raises(DBAPIError):  # transient types are never stored
        await attempt(
            ("UPDATE agent_sessions SET last_seq=last_seq+1 WHERE id=:i", {"i": sid}),
            (insert, {**base, "q": 2, "t": "stream.reset"}),
        )
    with pytest.raises(DBAPIError, match="immutable"):
        await attempt(("DELETE FROM agent_sessions WHERE id=:i", {"i": sid}))
    with pytest.raises(DBAPIError, match="identity is immutable"):
        await attempt(
            ("UPDATE agent_sessions SET principal_id=:p WHERE id=:i", {"i": sid, "p": uuid4()})
        )
    async with factory() as s, s.begin():
        assert await log.head(s, ctx.org_id, sid) == 1  # nothing above changed the log


async def test_a_finished_session_cannot_change_status_and_an_approval_decides_once(
    factory, tmp_path
):
    ctx, _log, _service, created = await new_session(factory, tmp_path)
    sid = created.id
    async with factory() as s, s.begin():
        await s.execute(
            text("UPDATE agent_sessions SET status='completed' WHERE id=:i"), {"i": sid}
        )
    with pytest.raises(DBAPIError, match="finished"):
        async with factory() as s, s.begin():
            await s.execute(
                text("UPDATE agent_sessions SET status='running' WHERE id=:i"), {"i": sid}
            )
    ins = (
        "INSERT INTO agent_approvals(org_id,session_id,approval_id,subject_kind,subject_ref,requested_seq,expires_at)"
        " VALUES(:o,:i,'a1','patch','p1',1,now()+interval '1 hour')"
    )
    async with factory() as s, s.begin():
        await s.execute(text(ins), {"o": ctx.org_id, "i": sid})
        await s.execute(
            text(
                "UPDATE agent_approvals SET status='approved', decided_at=now() WHERE approval_id='a1'"
            )
        )
    with pytest.raises(DBAPIError, match="decided once"):
        async with factory() as s, s.begin():
            await s.execute(
                text(
                    "UPDATE agent_approvals SET status='rejected', decided_at=now() WHERE approval_id='a1'"
                )
            )


async def test_checkpoints_append_with_an_expected_predecessor(factory, tmp_path):
    ctx, _log, _service, created = await new_session(factory, tmp_path)
    ins = (
        "INSERT INTO agent_session_checkpoints(org_id,session_id,sequence,state_digest,state_ciphertext)"
        " VALUES(:o,:i,:q,:d,decode(repeat('00',40),'hex'))"
    )
    params = {"o": ctx.org_id, "i": created.id, "d": "a" * 64}
    async with factory() as s, s.begin():
        await s.execute(text(ins), {**params, "q": 1})
        await s.execute(text(ins), {**params, "q": 2})
    for bad in (2, 4):
        with pytest.raises(DBAPIError, match=r"predecessor|duplicate"):
            async with factory() as s, s.begin():
                await s.execute(text(ins), {**params, "q": bad})
    with pytest.raises(DBAPIError, match="immutable"):
        async with factory() as s, s.begin():
            await s.execute(text("DELETE FROM agent_session_checkpoints WHERE sequence=1"))


def test_the_api_role_can_append_and_read_events_but_never_change_them():
    # Role grants come from the migration alone: the shared harness adds blanket SELECT grants.
    from scripts import phase2_db as harness

    values = {k: v for k, v in os.environ.items() if k.startswith("CODESPY_TEST_")}
    name = harness.create_database(values, "agent_roles")
    try:
        harness.migrate(values, name, "0050_agent_sessions")
        with harness.connect(values, name) as conn:

            def has(role, table, priv):
                return conn.execute(
                    "SELECT has_table_privilege(%s,%s,%s)", (role, f"public.{table}", priv)
                ).fetchone()[0]

            for role in ("p2_api", "p2_broker"):
                assert has(role, "agent_events", "SELECT") and has(role, "agent_events", "INSERT")
                assert not has(role, "agent_events", "UPDATE")
                assert not has(role, "agent_events", "DELETE")
                assert not has(role, "agent_sessions", "DELETE")
                assert not has(role, "agent_approvals", "DELETE")
            assert not conn.execute(
                "SELECT has_column_privilege('p2_api','public.agent_session_checkpoints','state_ciphertext','SELECT')"
            ).fetchone()[0]
            assert conn.execute(
                "SELECT has_column_privilege('p2_broker','public.agent_session_checkpoints','state_ciphertext','SELECT')"
            ).fetchone()[0]
            assert not conn.execute(
                "SELECT has_column_privilege('p2_api','public.agent_sessions','principal_id','UPDATE')"
            ).fetchone()[0]
    finally:
        harness.drop_database(values, name)


# --- replay and live delivery -----------------------------------------------------------------


async def test_a_stream_resumed_at_every_seq_equals_the_uninterrupted_stream(factory, tmp_path):
    ctx, log, _service, created = await new_session(factory, tmp_path)
    await record(factory, log, ctx, created.id)
    total = len(RECORDING)
    waiter = LocalWaiter()

    def stream(after):
        return tail(factory, log, waiter, ctx.org_id, created.id, after, poll_seconds=0.05)

    whole = await frames(stream(0), total)
    assert [seq_of(f) for f in whole] == list(range(1, total + 1))
    for cut in range(0, total + 1):
        head = await frames(stream(0), cut) if cut else []
        rest = await frames(stream(cut), total - cut) if cut < total else []
        assert head + rest == whole, f"disconnect after seq {cut}"
    for cut in (1, 7):  # Last-Event-ID semantics: resuming never repeats or skips
        assert [seq_of(f) for f in await frames(stream(cut), total - cut)] == list(
            range(cut + 1, total + 1)
        )


async def test_an_overdue_client_gets_a_reset_and_the_stream_ends(factory, tmp_path):
    ctx, log, _service, created = await new_session(factory, tmp_path)
    await record(factory, log, ctx, created.id)
    out = [
        f
        async for f in tail(
            factory,
            log,
            LocalWaiter(),
            ctx.org_id,
            created.id,
            0,
            replay_limit=3,
            poll_seconds=0.05,
        )
    ]
    assert (
        len(out) == 1 and out[0].startswith("event: stream.reset\n") and '"replay_limit"' in out[0]
    )
    assert "id:" not in out[0]  # the client keeps its Last-Event-ID and reloads /state


async def test_an_idle_stream_sends_heartbeats(factory, tmp_path):
    ctx, log, _service, created = await new_session(factory, tmp_path)
    got = await frames(
        tail(
            factory,
            log,
            LocalWaiter(),
            ctx.org_id,
            created.id,
            1,
            poll_seconds=0.02,
            heartbeat_seconds=0.05,
        ),
        2,
    )
    assert got == [": hb\n\n", ": hb\n\n"]


async def test_two_api_processes_deliver_the_same_live_stream(factory, engine, tmp_path):
    ctx, log, _service, created = await new_session(factory, tmp_path)
    dsn = asyncpg_dsn(engine.url)
    first, second = PgNotifyWaiter(dsn), PgNotifyWaiter(dsn)  # one LISTEN connection per process
    await first.start()
    await second.start()
    try:
        # A 60 s poll means only the database notification can wake these tails in time.
        tails = [
            asyncio.create_task(
                frames(
                    tail(
                        factory,
                        log,
                        w,
                        ctx.org_id,
                        created.id,
                        0,
                        poll_seconds=60,
                        heartbeat_seconds=120,
                    ),
                    6,
                )
            )
            for w in (first, second)
        ]
        await asyncio.sleep(0.3)
        await record(factory, log, ctx, created.id, count=6)  # a third process writes 5 events
        a, b = await asyncio.wait_for(asyncio.gather(*tails), timeout=8)
    finally:
        await first.stop()
        await second.stop()
    assert a == b and [seq_of(f) for f in a] == [1, 2, 3, 4, 5, 6]


async def test_a_tail_without_notifications_still_catches_up_by_polling(factory, tmp_path):
    ctx, log, _service, created = await new_session(factory, tmp_path)
    task = asyncio.create_task(
        frames(tail(factory, log, LocalWaiter(), ctx.org_id, created.id, 0, poll_seconds=0.05), 4)
    )
    await asyncio.sleep(0.1)
    await record(factory, log, ctx, created.id, count=4)
    got = await asyncio.wait_for(task, timeout=5)
    assert [seq_of(f) for f in got] == [1, 2, 3, 4]


def test_the_recording_uses_only_stored_event_types():
    assert all(kind in SAMPLES for kind, _turn, _parent in RECORDING)
    assert validate_payload("session.status", SAMPLES["session.status"])
    assert isinstance(UUID(str(uuid4())), UUID)
