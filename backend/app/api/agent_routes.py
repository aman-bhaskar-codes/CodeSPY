"""Agent session routes (AEP/1, section 5): create, read, state, paged events and the SSE stream."""

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from uuid import UUID

from app.contracts.agent_session import AgentSessionCreateRequestV1
from app.domain.scope import TenantContext
from app.models.agent_tables import agent_sessions
from app.services.agent.event_log import AgentEventLog, Waiter, tail
from app.services.agent.sessions import AgentSessionService
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

STREAM_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _after(request: Request, query_after: int | None) -> int:
    header = request.headers.get("last-event-id")
    if header is not None:
        if not header.isdigit() or len(header) > 18:
            raise HTTPException(422)
        return int(header)
    if query_after is not None and query_after < 0:
        raise HTTPException(422)
    return query_after or 0


def register_agent_routes(
    app: FastAPI,
    *,
    factory: async_sessionmaker[AsyncSession],
    log: AgentEventLog,
    waiter: Waiter,
    repository_context: Callable[..., Awaitable[TenantContext]],
    resource_context: Callable[..., Awaitable[TenantContext]],
    json_body: Callable[..., Awaitable[Any]],
    idempotency_key: Callable[[Request], str],
    poll_seconds: float = 2.0,
) -> AgentSessionService:
    service = AgentSessionService(factory, log)

    @app.post("/api/v1/agent-sessions", status_code=201)
    async def create_agent_session(request: Request) -> JSONResponse:
        body = await json_body(request, AgentSessionCreateRequestV1)
        ctx = await repository_context(request, body.repository_id, write=True)
        created = await service.create(ctx, body, idempotency_key(request))
        return JSONResponse(status_code=201, content=created.model_dump(mode="json"))

    @app.get("/api/v1/agent-sessions/{session_id}")
    async def read_agent_session(request: Request, session_id: UUID) -> dict[str, Any]:
        ctx = await resource_context(request, agent_sessions, session_id)
        return (await service.get(ctx, session_id)).model_dump(mode="json")

    @app.get("/api/v1/agent-sessions/{session_id}/state")
    async def read_agent_state(request: Request, session_id: UUID) -> dict[str, Any]:
        ctx = await resource_context(request, agent_sessions, session_id)
        return (await service.state(ctx, session_id)).model_dump(mode="json")

    @app.get("/api/v1/agent-sessions/{session_id}/events")
    async def read_agent_events(
        request: Request, session_id: UUID, after: int = 0, limit: int = 200
    ) -> dict[str, Any]:
        if after < 0 or not 1 <= limit <= 500:
            raise HTTPException(422)
        ctx = await resource_context(request, agent_sessions, session_id)
        events = await service.events(ctx, session_id, after, limit)
        return {"items": [e.model_dump(mode="json", exclude_none=True) for e in events]}

    @app.get("/api/v1/agent-sessions/{session_id}/stream")
    async def stream_agent_events(
        request: Request, session_id: UUID, after: int | None = None
    ) -> StreamingResponse:
        cursor = _after(request, after)
        ctx = await resource_context(request, agent_sessions, session_id)
        await service.get(ctx, session_id)  # 404 before any bytes are sent

        async def frames() -> AsyncIterator[str]:
            async for frame in tail(
                factory, log, waiter, ctx.org_id, session_id, cursor, poll_seconds=poll_seconds
            ):
                yield frame

        return StreamingResponse(frames(), media_type="text/event-stream", headers=STREAM_HEADERS)

    return service
