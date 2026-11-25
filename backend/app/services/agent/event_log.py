"""The append-only, gap-free agent event log and its live tail (AEP/1, sections 2 and 3).

Writers allocate ``seq`` by locking the session row and incrementing ``last_seq`` in the same
transaction that inserts the event, so a reader never sees an event the database does not hold
and the sequence has no gaps. Payloads are encrypted at rest with the event's identity as
associated data, so a row moved to another session or position fails to decrypt.
Readers pull rows after a sequence; a ``Waiter`` only decides when to look again, so a missed
notification delays an event but never loses one.
"""

import asyncio
import hashlib
import os
import time
from collections.abc import AsyncIterator, Callable, Coroutine
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Protocol
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.agent_events import (
    HEARTBEAT_FRAME,
    HEARTBEAT_SECONDS,
    REPLAY_LIMIT,
    TRANSIENT_TYPES,
    AgentEventV1,
    encode_payload,
    sse_frame,
    validate_payload,
)
from app.db.transaction import transaction
from app.domain.errors import DuplicateConflict, ScopeViolation
from app.models.agent_tables import agent_events, agent_sessions

NOTIFY_CHANNEL = "codespy_agent_events"
READ_BATCH = 200


class EventCipher:
    """AES-256-GCM. The key is injected; construction never reads the environment."""

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("agent event encryption requires a 256-bit key")
        self._aes = AESGCM(key)

    @staticmethod
    def _aad(org_id: UUID, session_id: UUID, seq: int, event_type: str) -> bytes:
        return f"codespy:agent-event:{org_id}:{session_id}:{seq}:{event_type}".encode()

    def encrypt(self, value: bytes, org_id: UUID, session_id: UUID, seq: int, kind: str) -> bytes:
        nonce = os.urandom(12)
        return nonce + self._aes.encrypt(nonce, value, self._aad(org_id, session_id, seq, kind))

    def decrypt(self, value: bytes, org_id: UUID, session_id: UUID, seq: int, kind: str) -> bytes:
        return self._aes.decrypt(value[:12], value[12:], self._aad(org_id, session_id, seq, kind))


def _required(value: datetime | None) -> datetime:
    if value is None:
        raise ScopeViolation("agent event was not stored")
    return value


def format_ts(value: datetime) -> str:
    value = value.astimezone(UTC)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


class AgentEventLog:
    def __init__(self, cipher: EventCipher) -> None:
        self.cipher = cipher

    async def append(
        self,
        s: AsyncSession,
        org_id: UUID,
        session_id: UUID,
        event_type: str,
        payload: dict[str, Any],
        *,
        turn: int = 0,
        parent: str | None = None,
        event_key: str | None = None,
    ) -> AgentEventV1:
        """Append one event inside the caller's transaction.

        With ``event_key`` the call is idempotent: a replay returns the stored event, and the same
        key with another type or payload is a conflict.
        """
        if event_type in TRANSIENT_TYPES:
            raise ValueError("transient events are delivered to one connection and never stored")
        canonical = validate_payload(event_type, payload)
        plaintext = encode_payload(canonical)
        locked = await s.scalar(
            select(agent_sessions.c.last_seq)
            .where(agent_sessions.c.org_id == org_id, agent_sessions.c.id == session_id)
            .with_for_update()
        )
        if locked is None:
            raise ScopeViolation("agent session unavailable")
        if event_key is not None:
            row = (
                await s.execute(
                    select(agent_events).where(
                        agent_events.c.org_id == org_id,
                        agent_events.c.session_id == session_id,
                        agent_events.c.event_key == event_key,
                    )
                )
            ).one_or_none()
            if row is not None:
                stored = self._decode(row)
                if stored.type != event_type or encode_payload(stored.payload) != plaintext:
                    raise DuplicateConflict("event key reused with different content")
                return stored
        seq = int(
            await s.scalar(
                update(agent_sessions)
                .where(agent_sessions.c.org_id == org_id, agent_sessions.c.id == session_id)
                .values(last_seq=agent_sessions.c.last_seq + 1)
                .returning(agent_sessions.c.last_seq)
            )
            or 0
        )
        created = await s.scalar(
            insert(agent_events)
            .values(
                org_id=org_id,
                session_id=session_id,
                seq=seq,
                type=event_type,
                turn=turn,
                parent=parent,
                event_key=event_key,
                payload_ciphertext=self.cipher.encrypt(
                    plaintext, org_id, session_id, seq, event_type
                ),
                payload_bytes=len(plaintext),
            )
            .returning(agent_events.c.created_at)
        )
        return AgentEventV1(
            session_id=str(session_id),
            seq=seq,
            type=event_type,
            ts=format_ts(_required(created)),
            turn=turn,
            parent=parent,
            payload=canonical,
        )

    async def read_after(
        self, s: AsyncSession, org_id: UUID, session_id: UUID, after: int, limit: int = READ_BATCH
    ) -> list[AgentEventV1]:
        rows = (
            await s.execute(
                select(agent_events)
                .where(
                    agent_events.c.org_id == org_id,
                    agent_events.c.session_id == session_id,
                    agent_events.c.seq > after,
                )
                .order_by(agent_events.c.seq)
                .limit(limit)
            )
        ).all()
        return [self._decode(r) for r in rows]

    @staticmethod
    async def head(s: AsyncSession, org_id: UUID, session_id: UUID) -> int | None:
        value = await s.scalar(
            select(agent_sessions.c.last_seq).where(
                agent_sessions.c.org_id == org_id, agent_sessions.c.id == session_id
            )
        )
        return None if value is None else int(value)

    def _decode(self, row: Any) -> AgentEventV1:
        import json

        plaintext = self.cipher.decrypt(
            bytes(row.payload_ciphertext), row.org_id, row.session_id, row.seq, row.type
        )
        return AgentEventV1(
            session_id=str(row.session_id),
            seq=row.seq,
            type=row.type,
            ts=format_ts(row.created_at),
            turn=row.turn,
            parent=row.parent,
            payload=json.loads(plaintext),
        )


def digest_events(events: list[AgentEventV1]) -> str:
    """A stable digest of an event sequence, for replay equality checks."""
    h = hashlib.sha256()
    for e in events:
        h.update(e.model_dump_json(exclude_none=True).encode())
        h.update(b"\n")
    return h.hexdigest()


# --- waking a tail ------------------------------------------------------------------------------


class Subscription(Protocol):
    async def __aenter__(self) -> "Subscription": ...
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...
    async def wait(self, seconds: float) -> bool:
        """Block until a notification arrives for the session or the timeout passes."""
        ...


class Waiter(Protocol):
    def subscribe(self, session_id: UUID) -> Subscription: ...


class _LocalSubscription:
    def __init__(self, owner: "LocalWaiter", session_id: UUID) -> None:
        self._owner, self._key = owner, str(session_id)
        self._event = asyncio.Event()

    async def __aenter__(self) -> "_LocalSubscription":
        self._owner._subs.setdefault(self._key, set()).add(self._event)
        return self

    async def __aexit__(self, *_: object) -> None:
        subs = self._owner._subs.get(self._key)
        if subs is not None:
            subs.discard(self._event)
            if not subs:
                del self._owner._subs[self._key]

    async def wait(self, seconds: float) -> bool:
        try:
            await asyncio.wait_for(self._event.wait(), seconds)
        except TimeoutError:
            return False
        self._event.clear()
        return True


class LocalWaiter:
    """Wakes tails inside one process. Tests use it directly; ``PgNotifyWaiter`` feeds it."""

    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Event]] = {}

    def subscribe(self, session_id: UUID) -> _LocalSubscription:
        return _LocalSubscription(self, session_id)

    def notify(self, session_id: str) -> None:
        for event in list(self._subs.get(session_id, ())):
            event.set()


class PgNotifyWaiter(LocalWaiter):
    """One LISTEN connection per process; every insert notifies every API process."""

    def __init__(self, dsn: str) -> None:
        super().__init__()
        self._dsn = dsn
        self._connection: Any = None

    async def start(self) -> None:
        self._connection = await asyncpg.connect(self._dsn)
        await self._connection.add_listener(NOTIFY_CHANNEL, self._on_notify)

    async def stop(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    def _on_notify(self, _connection: Any, _pid: int, _channel: str, payload: str) -> None:
        self.notify(payload.split(":", 1)[0])


def asyncpg_dsn(url: Any) -> str:
    """A DSN for the listener, from the SQLAlchemy URL the application already trusts."""
    return str(url.set(drivername="postgresql").render_as_string(hide_password=False))


async def _uncancellable[T](work: Coroutine[Any, Any, T]) -> T:
    """Let a short database read finish even if the client disconnects meanwhile.

    A cancelled asyncpg query leaves its pooled connection in an unusable state; finishing the read
    returns the connection cleanly. The caller still sees the cancellation as soon as the read ends.
    """
    task = asyncio.ensure_future(work)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        task.add_done_callback(lambda t: t.cancelled() or t.exception())  # consume, never log
        raise


# --- the tail -----------------------------------------------------------------------------------


def reset_frame(session_id: UUID, reason: str) -> str:
    return sse_frame(
        AgentEventV1(
            session_id=str(session_id),
            seq=0,
            type="stream.reset",
            ts=format_ts(datetime.now(UTC)),
            turn=0,
            payload={"reason": reason},
        )
    )


async def tail(
    factory: async_sessionmaker[AsyncSession],
    log: AgentEventLog,
    waiter: Waiter,
    org_id: UUID,
    session_id: UUID,
    after: int,
    *,
    poll_seconds: float = 2.0,
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
    replay_limit: int = REPLAY_LIMIT,
    clock: Callable[[], float] = time.monotonic,
) -> AsyncIterator[str]:
    """SSE frames after ``after``: replay, then live. Ends only on reset or disconnect.

    The subscription is created before the first read, so an event committed between a read and
    the wait is never missed. If the client is more than ``replay_limit`` events behind, the
    tail sends ``stream.reset`` and ends; the client loads ``/state`` and reconnects.
    """
    last_sent = after
    last_write = clock()

    async def read_page(cursor: int) -> tuple[int | None, list[AgentEventV1]]:
        async with transaction(factory) as s:
            head = await log.head(s, org_id, session_id)
            if head is None or head - cursor > replay_limit:
                return head, []
            return head, await log.read_after(s, org_id, session_id, cursor)

    async with waiter.subscribe(session_id) as sub:
        while True:
            head, rows = await _uncancellable(read_page(last_sent))
            if head is None:
                raise ScopeViolation("agent session unavailable")
            if head - last_sent > replay_limit:
                yield reset_frame(
                    session_id, "replay_limit" if last_sent == after else "backpressure"
                )
                return
            if rows:
                for event in rows:
                    yield sse_frame(event)
                    last_sent = event.seq
                last_write = clock()
                continue
            await sub.wait(poll_seconds)
            if clock() - last_write >= heartbeat_seconds:
                yield HEARTBEAT_FRAME
                last_write = clock()
