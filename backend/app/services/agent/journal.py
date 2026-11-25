"""The agent effect journal: what the loop decided and did, as encrypted append-only rows.

Each effect (a model turn, or one tool call) has up to three records. ``intent`` is what the loop
planned, ``started`` marks that execution began, and ``receipt`` is the result. Records are written
once; writing the same record again is harmless and writing a different one is a conflict. A
restarted loop reads them to decide what is finished, what can run, and what can only be reported
as unknown.
"""

import json
import os
from typing import Any, Literal
from uuid import UUID

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.transaction import transaction
from app.domain.errors import DuplicateConflict
from app.models.agent_tables import agent_effects

Phase = Literal["intent", "started", "receipt"]
Kind = Literal["model", "tool"]
MAX_PAYLOAD_BYTES = 2 * 1024 * 1024 - 64


class JournalCipher:
    """AES-256-GCM bound to the record's identity, so a row moved elsewhere fails to decrypt."""

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("agent journal encryption requires a 256-bit key")
        self._aes = AESGCM(key)

    @staticmethod
    def _aad(org: UUID, session: UUID, effect_id: str, phase: str) -> bytes:
        return f"codespy:agent-effect:{org}:{session}:{effect_id}:{phase}".encode()

    def encrypt(self, value: bytes, org: UUID, session: UUID, effect_id: str, phase: str) -> bytes:
        nonce = os.urandom(12)
        return nonce + self._aes.encrypt(nonce, value, self._aad(org, session, effect_id, phase))

    def decrypt(self, value: bytes, org: UUID, session: UUID, effect_id: str, phase: str) -> bytes:
        return self._aes.decrypt(value[:12], value[12:], self._aad(org, session, effect_id, phase))


def _encode(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


class AgentJournal:
    def __init__(self, factory: async_sessionmaker[AsyncSession], cipher: JournalCipher) -> None:
        self.factory, self.cipher = factory, cipher

    async def put(
        self,
        org: UUID,
        session: UUID,
        effect_id: str,
        phase: Phase,
        kind: Kind,
        payload: dict[str, Any],
    ) -> bool:
        """Record once. ``True`` if written now, ``False`` if the same record was already there."""
        plaintext = _encode(payload)
        if len(plaintext) > MAX_PAYLOAD_BYTES:
            raise ValueError("journal record is too large")
        async with transaction(self.factory) as s:
            cipher = self.cipher.encrypt(plaintext, org, session, effect_id, phase)
            inserted = await s.scalar(
                insert(agent_effects)
                .values(
                    org_id=org,
                    session_id=session,
                    effect_id=effect_id,
                    phase=phase,
                    kind=kind,
                    payload_ciphertext=cipher,
                    payload_bytes=len(plaintext),
                )
                .on_conflict_do_nothing()
                .returning(agent_effects.c.effect_id)
            )
            if inserted is not None:
                return True
            existing = await self._read(s, org, session, effect_id, phase)
            if existing is None or _encode(existing) != plaintext:
                raise DuplicateConflict("journal record differs from the one already written")
            return False

    async def get(
        self, org: UUID, session: UUID, effect_id: str, phase: Phase
    ) -> dict[str, Any] | None:
        async with transaction(self.factory) as s:
            return await self._read(s, org, session, effect_id, phase)

    async def phases(self, org: UUID, session: UUID, effect_id: str) -> dict[str, dict[str, Any]]:
        """Every record of one effect, by phase."""
        async with transaction(self.factory) as s:
            rows = (
                (
                    await s.execute(
                        select(agent_effects.c.phase).where(
                            agent_effects.c.org_id == org,
                            agent_effects.c.session_id == session,
                            agent_effects.c.effect_id == effect_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            out: dict[str, dict[str, Any]] = {}
            for phase in rows:
                record = await self._read(s, org, session, effect_id, phase)
                if record is not None:
                    out[phase] = record
            return out

    async def count(self, org: UUID, session: UUID, kind: Kind, phase: Phase = "receipt") -> int:
        async with transaction(self.factory) as s:
            return int(
                await s.scalar(
                    select(func.count()).where(
                        agent_effects.c.org_id == org,
                        agent_effects.c.session_id == session,
                        agent_effects.c.kind == kind,
                        agent_effects.c.phase == phase,
                    )
                )
                or 0
            )

    async def receipts(
        self, org: UUID, session: UUID, kind: Kind
    ) -> list[tuple[str, dict[str, Any]]]:
        """Every receipt of a kind, oldest first, with its effect id."""
        async with transaction(self.factory) as s:
            rows = (
                await s.execute(
                    select(agent_effects.c.effect_id, agent_effects.c.payload_ciphertext)
                    .where(
                        agent_effects.c.org_id == org,
                        agent_effects.c.session_id == session,
                        agent_effects.c.kind == kind,
                        agent_effects.c.phase == "receipt",
                    )
                    .order_by(agent_effects.c.created_at, agent_effects.c.effect_id)
                )
            ).all()
            return [
                (
                    r.effect_id,
                    json.loads(
                        self.cipher.decrypt(
                            bytes(r.payload_ciphertext), org, session, r.effect_id, "receipt"
                        )
                    ),
                )
                for r in rows
            ]

    async def _read(
        self, s: AsyncSession, org: UUID, session: UUID, effect_id: str, phase: str
    ) -> dict[str, Any] | None:
        row = (
            await s.execute(
                select(agent_effects.c.payload_ciphertext).where(
                    agent_effects.c.org_id == org,
                    agent_effects.c.session_id == session,
                    agent_effects.c.effect_id == effect_id,
                    agent_effects.c.phase == phase,
                )
            )
        ).first()
        if row is None:
            return None
        decoded: dict[str, Any] = json.loads(
            self.cipher.decrypt(bytes(row.payload_ciphertext), org, session, effect_id, phase)
        )
        return decoded
