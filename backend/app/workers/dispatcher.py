"""Claim outbox intent, send without DB locks, then CAS the claim token."""

from datetime import timedelta
from uuid import uuid4

from app.db.time import database_now
from app.db.transaction import transaction
from app.domain.canonical_json import canonical_bytes
from app.models.phase2_tables import PHASE2_TABLES
from app.ports.queue import QueuePort
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

outbox = PHASE2_TABLES["outbox"]


class OutboxDispatcher:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        queue: QueuePort,
        *,
        dead_letters: QueuePort | None = None,
    ):
        self.factory = factory
        self.queue = queue
        self.dead_letters = dead_letters

    async def dispatch_once(self, limit: int = 20) -> int:
        if not 1 <= limit <= 20:
            raise ValueError("invalid outbox batch")
        async with transaction(self.factory) as s:
            rows = (
                (
                    await s.execute(
                        select(outbox)
                        .where(
                            outbox.c.next_send_at <= func.clock_timestamp(),
                            or_(
                                outbox.c.state == "pending",
                                and_(
                                    outbox.c.state == "claimed",
                                    outbox.c.claim_expires_at <= func.clock_timestamp(),
                                ),
                            ),
                        )
                        .order_by(outbox.c.next_send_at, outbox.c.org_id, outbox.c.id)
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                )
                .mappings()
                .all()
            )
            claimed = []
            for stored in rows:
                token = uuid4().hex
                await s.execute(
                    update(outbox)
                    .where(outbox.c.org_id == stored["org_id"], outbox.c.id == stored["id"])
                    .values(
                        state="claimed",
                        claim_token=token,
                        claim_expires_at=func.clock_timestamp() + timedelta(seconds=30),
                        send_attempts=stored["send_attempts"] + 1,
                    )
                )
                claimed.append((dict(stored), token))
        sent = 0
        for row, token in claimed:
            failed = False
            try:
                queue = self.dead_letters if row["kind"] == "dead_letter" else self.queue
                if queue is None:
                    raise RuntimeError("diagnostic queue unavailable")
                await queue.send(canonical_bytes(row["payload"]))
            except Exception:
                failed = True
            async with transaction(self.factory) as s:
                if failed:
                    await s.execute(
                        update(outbox)
                        .where(
                            outbox.c.org_id == row["org_id"],
                            outbox.c.id == row["id"],
                            outbox.c.state == "claimed",
                            outbox.c.claim_token == token,
                        )
                        .values(
                            state="pending",
                            claim_token=None,
                            claim_expires_at=None,
                            next_send_at=await database_now(s)
                            + timedelta(seconds=min(60, 5 * 2 ** min(row["send_attempts"], 4))),
                        )
                    )
                else:
                    result = await s.scalars(
                        update(outbox)
                        .where(
                            outbox.c.org_id == row["org_id"],
                            outbox.c.id == row["id"],
                            outbox.c.state == "claimed",
                            outbox.c.claim_token == token,
                        )
                        .values(
                            state="sent",
                            sent_at=func.clock_timestamp(),
                            claim_token=None,
                            claim_expires_at=None,
                        )
                        .returning(outbox.c.id)
                    )
                    sent += len(result.all())
        return sent
