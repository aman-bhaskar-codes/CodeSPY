"""Opaque browser sessions. Provider tokens and organization roles are not cookies."""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from app.db.time import database_now
from app.db.transaction import transaction
from app.models.phase2_tables import PHASE2_TABLES
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

sessions = PHASE2_TABLES["sessions"]


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class IssuedSession:
    token: str
    csrf: str
    principal_id: UUID


class SessionService:
    def __init__(self, factory: async_sessionmaker[AsyncSession]):
        self.factory = factory

    async def issue(self, principal_id: UUID, *, previous: str | None = None) -> IssuedSession:
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        async with transaction(self.factory) as s:
            now = await database_now(s)
            if previous is not None:
                await s.execute(
                    update(sessions)
                    .where(sessions.c.token_hash == token_digest(previous))
                    .values(revoked_at=now)
                )
            await s.execute(
                insert(sessions).values(
                    id=uuid4(),
                    principal_id=principal_id,
                    token_hash=token_digest(token),
                    csrf_hash=token_digest(csrf),
                    issued_at=now,
                    last_seen_at=now,
                    idle_expires_at=now + timedelta(minutes=30),
                    absolute_expires_at=now + timedelta(hours=24),
                )
            )
        return IssuedSession(token=token, csrf=csrf, principal_id=principal_id)

    async def resolve(self, token: str | None) -> tuple[UUID, str] | None:
        if token is None or not 32 <= len(token) <= 128:
            return None
        async with transaction(self.factory) as s:
            row = (
                (
                    await s.execute(
                        select(sessions).where(sessions.c.token_hash == token_digest(token))
                    )
                )
                .mappings()
                .one_or_none()
            )
            now = await database_now(s)
            if (
                row is None
                or row["revoked_at"] is not None
                or min(row["idle_expires_at"], row["absolute_expires_at"]) <= now
            ):
                return None
            if row["last_seen_at"] <= now - timedelta(minutes=1):
                await s.execute(
                    update(sessions)
                    .where(sessions.c.id == row["id"], sessions.c.revoked_at.is_(None))
                    .values(
                        last_seen_at=now,
                        idle_expires_at=min(
                            now + timedelta(minutes=30), row["absolute_expires_at"]
                        ),
                    )
                )
            return row["principal_id"], row["csrf_hash"]

    async def renew_csrf(self, token: str) -> str:
        csrf = secrets.token_urlsafe(32)
        async with transaction(self.factory) as s:
            await s.execute(
                update(sessions)
                .where(
                    sessions.c.token_hash == token_digest(token), sessions.c.revoked_at.is_(None)
                )
                .values(csrf_hash=token_digest(csrf))
            )
        return csrf

    async def revoke(self, token: str) -> None:
        async with transaction(self.factory) as s:
            await s.execute(
                update(sessions)
                .where(
                    sessions.c.token_hash == token_digest(token), sessions.c.revoked_at.is_(None)
                )
                .values(revoked_at=await database_now(s))
            )
