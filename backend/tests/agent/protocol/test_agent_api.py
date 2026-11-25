# ruff: noqa: E501
"""HTTP surface of the agent protocol: create, read, state, events, and the SSE stream over real sockets."""

import asyncio
import os
import socket
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn
from app.api.application import ApiSettings, ApplicationPorts, create_app
from app.security.sessions import SessionService
from app.services.agent.event_log import (
    AgentEventLog,
    EventCipher,
    PgNotifyWaiter,
    asyncpg_dsn,
)

from tests.agent.protocol.samples import RECORDING, SAMPLES
from tests.ast_facts.test_ast_facts_publication import admitted

pytestmark = pytest.mark.skipif(
    os.environ.get("CODESPY_TEST_SCHEMA_TARGET") != "head",
    reason="requires the current schema head (revision 0050)",
)

CIPHER = EventCipher(bytes(range(32)))


async def ready():
    return True


def settings():
    return ApiSettings(environment="test", auth_mode="local_fixture", origin="http://testserver")


async def asgi_client(factory, tmp_path):
    ctx, snapshot, repo, store = await admitted(factory, tmp_path)
    issued = await SessionService(factory).issue(ctx.principal_id)
    app = create_app(
        settings(),
        ApplicationPorts(
            factory, store, ctx, readiness=ready, agent_event_cipher=CIPHER, agent_poll_seconds=0.05
        ),
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        cookies={"codespy_dev": issued.token},
    )
    token = (await client.get("/api/v1/session")).json()["csrf_token"]
    headers = {"Origin": "http://testserver", "X-CSRF-Token": token, "Idempotency-Key": "s-1"}
    return client, headers, ctx, repo, snapshot, store, issued


async def test_create_read_state_and_page_events_over_http(factory, tmp_path):
    client, headers, ctx, repo, snapshot, _store, _issued = await asgi_client(factory, tmp_path)
    body = {"repository_id": str(repo), "snapshot_id": str(snapshot), "mode": "plan"}
    try:
        assert (
            await client.post("/api/v1/agent-sessions", json=body)
        ).status_code == 403  # no CSRF
        created = await client.post("/api/v1/agent-sessions", json=body, headers=headers)
        assert created.status_code == 201, created.text
        session = created.json()
        assert (
            session["last_seq"] == 1
            and session["status"] == "idle"
            and session["model_profile"] == "agent"
        )

        again = await client.post("/api/v1/agent-sessions", json=body, headers=headers)
        assert again.status_code == 201 and again.json()["id"] == session["id"]
        clash = await client.post(
            "/api/v1/agent-sessions", json={**body, "mode": "act"}, headers=headers
        )
        assert clash.status_code == 409 and clash.json()["code"] == "idempotency_conflict"
        bad = await client.post(
            "/api/v1/agent-sessions", json={**body, "extra": 1}, headers=headers
        )
        assert bad.status_code == 422
        other = await client.post(
            "/api/v1/agent-sessions",
            json={**body, "repository_id": "10000000-0000-4000-8000-0000000000aa"},
            headers={**headers, "Idempotency-Key": "s-2"},
        )
        assert other.status_code == 404

        sid = session["id"]
        assert (await client.get(f"/api/v1/agent-sessions/{sid}")).json()["id"] == sid
        log = AgentEventLog(CIPHER)
        for kind, turn, parent in RECORDING[1:6]:
            async with factory() as s, s.begin():
                await log.append(
                    s,
                    ctx.org_id,
                    session_id=__import__("uuid").UUID(sid),
                    event_type=kind,
                    payload=SAMPLES[kind],
                    turn=turn,
                    parent=parent,
                )
        state = (await client.get(f"/api/v1/agent-sessions/{sid}/state")).json()
        assert (
            state["as_of_seq"] == 6
            and state["status"] == "running"
            and state["messages"][-1]["completed"] is True
        )

        page = (
            await client.get(
                f"/api/v1/agent-sessions/{sid}/events", params={"after": 2, "limit": 2}
            )
        ).json()["items"]
        assert (
            [e["seq"] for e in page] == [3, 4]
            and page[0]["v"] == 1
            and page[0]["type"] == "session.status"
        )
        for query in ({"after": -1}, {"limit": 0}, {"limit": 501}):
            assert (
                await client.get(f"/api/v1/agent-sessions/{sid}/events", params=query)
            ).status_code == 422

        unknown = "10000000-0000-4000-8000-0000000000bb"
        for path in ("", "/state", "/events", "/stream"):
            assert (await client.get(f"/api/v1/agent-sessions/{unknown}{path}")).status_code == 404
        assert (
            await client.get(
                f"/api/v1/agent-sessions/{sid}/stream", headers={"Last-Event-ID": "abc"}
            )
        ).status_code == 422
        negative = await client.get(f"/api/v1/agent-sessions/{sid}/stream?after=-1")
        assert negative.status_code == 422
    finally:
        await client.aclose()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@asynccontextmanager
async def serving(app):
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    )
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


async def read_stream(base, cookie, session_id, count, last_event_id=None):
    headers = {"Host": "testserver"}
    if last_event_id is not None:
        headers["Last-Event-ID"] = str(last_event_id)
    ids, types = [], []
    async with (
        httpx.AsyncClient(cookies={"codespy_dev": cookie}, timeout=15) as client,
        client.stream(
            "GET", f"{base}/api/v1/agent-sessions/{session_id}/stream", headers=headers
        ) as response,
    ):
        if response.status_code != 200:
            raise AssertionError((response.status_code, await response.aread()))
        assert response.headers["content-type"].startswith("text/event-stream")
        assert (
            response.headers["cache-control"] == "no-cache"
            and response.headers["x-accel-buffering"] == "no"
        )
        async for line in response.aiter_lines():
            if line.startswith("id: "):
                ids.append(int(line[4:]))
            elif line.startswith("event: "):
                types.append(line[7:])
            if len(types) == count:  # the event line follows the id line
                break
    return ids, types


async def test_two_servers_stream_the_same_events_over_sse_and_resume_with_last_event_id(
    factory, engine, tmp_path
):
    ctx, snapshot, repo, store = await admitted(factory, tmp_path)
    issued = await SessionService(factory).issue(ctx.principal_id)
    dsn = asyncpg_dsn(engine.url)
    waiters = [PgNotifyWaiter(dsn), PgNotifyWaiter(dsn)]
    for w in waiters:
        await w.start()
    apps = [
        create_app(
            settings(),
            ApplicationPorts(
                factory,
                store,
                ctx,
                readiness=ready,
                agent_event_cipher=CIPHER,
                agent_waiter=w,
                agent_poll_seconds=60,
            ),
        )
        for w in waiters
    ]
    try:
        async with serving(apps[0]) as first, serving(apps[1]) as second:
            created = await httpx.AsyncClient(
                transport=httpx.ASGITransport(app=apps[0]),
                base_url="http://testserver",
                cookies={"codespy_dev": issued.token},
            ).post(
                "/api/v1/agent-sessions",
                json={"repository_id": str(repo), "snapshot_id": str(snapshot)},
                headers={
                    "Origin": "http://testserver",
                    "X-CSRF-Token": (
                        await httpx.AsyncClient(
                            transport=httpx.ASGITransport(app=apps[0]),
                            base_url="http://testserver",
                            cookies={"codespy_dev": issued.token},
                        ).get("/api/v1/session")
                    ).json()["csrf_token"],
                    "Idempotency-Key": "stream-1",
                },
            )
            session_id = created.json()["id"]
            total = len(RECORDING)
            readers = [
                asyncio.create_task(read_stream(base, issued.token, session_id, total))
                for base in (first, second)
            ]
            await asyncio.sleep(0.4)
            log = AgentEventLog(CIPHER)
            sid = __import__("uuid").UUID(session_id)
            for kind, turn, parent in RECORDING[1:]:
                async with factory() as s, s.begin():
                    await log.append(
                        s, ctx.org_id, sid, kind, SAMPLES[kind], turn=turn, parent=parent
                    )
            (ids_a, types_a), (ids_b, types_b) = await asyncio.wait_for(
                asyncio.gather(*readers), timeout=15
            )
            assert ids_a == ids_b == list(range(1, total + 1))
            assert types_a == types_b == [kind for kind, _t, _p in RECORDING]
            # A browser that dropped after seq 5 reconnects to the other server and gets exactly the rest.
            resumed_ids, resumed_types = await read_stream(
                second, issued.token, session_id, total - 5, last_event_id=5
            )
            assert resumed_ids == list(range(6, total + 1)) and resumed_types == types_a[5:]
            # ?after= works the same for clients that cannot set headers.
            async with (
                httpx.AsyncClient(cookies={"codespy_dev": issued.token}, timeout=15) as client,
                client.stream(
                    "GET",
                    f"{first}/api/v1/agent-sessions/{session_id}/stream?after={total - 1}",
                    headers={"Host": "testserver"},
                ) as response,
            ):
                lines = [line async for line in _first_lines(response, 1)]
            assert lines == [f"id: {total}"]
    finally:
        for w in waiters:
            await w.stop()


async def _first_lines(response, n):
    count = 0
    async for line in response.aiter_lines():
        yield line
        count += 1
        if count == n:
            return
