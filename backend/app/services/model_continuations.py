"""Tenant-bound encrypted transport state; never expose opaque items to tools."""

import json
import os
from datetime import UTC, timedelta
from uuid import UUID, uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.time import database_now
from app.domain.canonical_json import semantic_digest
from app.domain.errors import ScopeViolation
from app.domain.scope import TenantContext
from app.models.model_gateway_tables import model_reservations
from app.models.model_transport_tables import model_continuations
from app.security.authorization import require_repository


class ContinuationCipher:
    """An injected key; construction never loads environment or credential files."""

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("continuation encryption requires a 256-bit key")
        self._aes = AESGCM(key)

    def encrypt(self, value: bytes, context: str) -> bytes:
        nonce = os.urandom(12)
        return nonce + self._aes.encrypt(nonce, value, ("codespy:model:" + context).encode())

    def decrypt(self, value: bytes, context: str) -> bytes:
        return self._aes.decrypt(value[:12], value[12:], ("codespy:model:" + context).encode())


class ModelContinuationService:
    def __init__(self, cipher: ContinuationCipher) -> None:
        self.cipher = cipher

    @staticmethod
    def _context(
        org: UUID,
        repo: UUID,
        investigation: UUID,
        principal: UUID,
        identity: UUID,
        profile_digest: str,
    ) -> str:
        return semantic_digest(
            [str(org), str(repo), str(investigation), str(principal), str(identity), profile_digest]
        )

    async def put(
        self,
        session: AsyncSession,
        actor: TenantContext,
        repository_id: UUID,
        investigation_id: UUID,
        reservation_id: UUID,
        profile_digest: str,
        payload_json: str,
    ) -> UUID:
        await require_repository(
            session, actor.org_id, actor.principal_id, repository_id, write=True
        )
        if len(payload_json.encode()) > 262144:
            raise ValueError("continuation exceeds byte limit")
        if not isinstance(json.loads(payload_json), list):
            raise ValueError("continuation requires protocol item list")
        parent = (
            (
                await session.execute(
                    select(model_reservations).where(
                        model_reservations.c.org_id == actor.org_id,
                        model_reservations.c.reservation_id == reservation_id,
                        model_reservations.c.repository_id == repository_id,
                        model_reservations.c.investigation_id == investigation_id,
                        model_reservations.c.principal_id == actor.principal_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if parent is None:
            raise ScopeViolation("continuation unavailable")
        identity = uuid4()
        now = await database_now(session)
        context = self._context(
            actor.org_id,
            repository_id,
            investigation_id,
            actor.principal_id,
            identity,
            profile_digest,
        )
        cipher = self.cipher.encrypt(payload_json.encode(), context)
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            await session.execute(
                text(
                    "SELECT public.codespy_put_model_continuation("
                    ":org,:id,:repo,:res,:inv,:actor,:profile,:cipher,:expiry)"
                ),
                {
                    "org": actor.org_id,
                    "id": identity,
                    "repo": repository_id,
                    "res": reservation_id,
                    "inv": investigation_id,
                    "actor": actor.principal_id,
                    "profile": profile_digest,
                    "cipher": cipher,
                    "expiry": now + timedelta(days=7),
                },
            )
        else:
            await session.execute(
                insert(model_continuations).values(
                    org_id=actor.org_id,
                    id=identity,
                    repository_id=repository_id,
                    investigation_id=investigation_id,
                    reservation_id=reservation_id,
                    principal_id=actor.principal_id,
                    profile_digest=profile_digest,
                    ciphertext=self.cipher.encrypt(payload_json.encode(), context),
                    expires_at=now + timedelta(days=7),
                )
            )
        return identity

    async def get(
        self,
        session: AsyncSession,
        actor: TenantContext,
        repository_id: UUID,
        investigation_id: UUID,
        identity: UUID,
        profile_digest: str,
    ) -> str:
        await require_repository(
            session, actor.org_id, actor.principal_id, repository_id, write=True
        )
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT * FROM public.codespy_get_model_continuation("
                            ":org,:repo,:inv,:actor,:id,:profile)"
                        ),
                        {
                            "org": actor.org_id,
                            "repo": repository_id,
                            "inv": investigation_id,
                            "actor": actor.principal_id,
                            "id": identity,
                            "profile": profile_digest,
                        },
                    )
                )
                .mappings()
                .one_or_none()
            )
        else:
            row = (
                (
                    await session.execute(
                        select(model_continuations).where(
                            model_continuations.c.org_id == actor.org_id,
                            model_continuations.c.id == identity,
                            model_continuations.c.repository_id == repository_id,
                            model_continuations.c.investigation_id == investigation_id,
                            model_continuations.c.principal_id == actor.principal_id,
                            model_continuations.c.profile_digest == profile_digest,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        now = await database_now(session)
        if row is None:
            raise ScopeViolation("continuation unavailable")
        expiry = row["expires_at"]
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        if expiry <= now:
            raise ScopeViolation("continuation unavailable")
        context = self._context(
            actor.org_id,
            repository_id,
            investigation_id,
            actor.principal_id,
            identity,
            profile_digest,
        )
        return self.cipher.decrypt(row["ciphertext"], context).decode()
