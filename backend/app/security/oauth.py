"""One-use server-side OAuth flows and context-bound encrypted provider grants.

No configuration is read here. Live credentials remain an owner-operated setup.
"""

import base64
import hashlib
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from urllib.parse import unquote
from uuid import UUID, uuid4

from app.db.time import database_now
from app.db.transaction import transaction
from app.models.phase2_tables import PHASE2_TABLES
from app.models.tables import memberships, organizations, principals
from app.security.sessions import token_digest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class OAuthRejected(Exception):  # noqa: N818 - protocol rejection
    pass


@dataclass(frozen=True)
class ProviderIdentity:
    subject: int
    display_name: str
    token: str
    expires_at: datetime


class OAuthProvider(Protocol):
    def authorization_url(self, state: str, challenge: str) -> str: ...
    async def exchange(self, code: str, verifier: str) -> ProviderIdentity: ...


@dataclass(frozen=True)
class BegunFlow:
    state: str
    browser: str
    url: str


class GrantCipher:
    def __init__(self, key_id: str, key: bytes):
        if not key_id or len(key_id) > 128 or len(key) != 32:
            raise ValueError("explicit AES-256 key required")
        self.key_id = key_id
        self.aes = AESGCM(key)

    def encrypt(self, value: bytes, context: str) -> bytes:
        nonce = os.urandom(12)
        return nonce + self.aes.encrypt(nonce, value, ("github:" + context).encode())

    def decrypt(self, value: bytes, context: str) -> bytes:
        return self.aes.decrypt(value[:12], value[12:], ("github:" + context).encode())


class OAuthService:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        provider: OAuthProvider,
        cipher: GrantCipher,
    ):
        self.factory, self.provider, self.cipher = factory, provider, cipher

    async def begin(self, return_path: str) -> BegunFlow:
        decoded = unquote(return_path)
        if (
            len(return_path) > 512
            or not decoded.startswith("/")
            or decoded.startswith("//")
            or "\\" in decoded
            or any(ord(c) < 32 or ord(c) == 127 for c in decoded)
            or decoded.split("?")[0] not in {"/", "/repositories"}
        ):
            raise ValueError("allowlisted relative return path required")
        state, browser, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        identity = uuid4()
        async with transaction(self.factory) as session:
            now = await database_now(session)
            await session.execute(
                insert(PHASE2_TABLES["oauth_flows"]).values(
                    id=identity,
                    state_hash=token_digest(state),
                    browser_hash=token_digest(browser),
                    verifier_cipher=self.cipher.encrypt(verifier.encode(), str(identity)),
                    key_id=self.cipher.key_id,
                    return_path=return_path,
                    expires_at=now + timedelta(minutes=10),
                )
            )
        return BegunFlow(state, browser, self.provider.authorization_url(state, challenge))

    async def callback(self, state: str, browser: str, code: str) -> UUID:
        if not all(0 < len(value) <= 4096 for value in (state, browser, code)):
            raise OAuthRejected("invalid callback")
        flows = PHASE2_TABLES["oauth_flows"]
        async with transaction(self.factory) as session:
            row = (
                (
                    await session.execute(
                        select(flows)
                        .where(flows.c.state_hash == token_digest(state))
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            now = await database_now(session)
            if (
                row is None
                or row["consumed_at"] is not None
                or row["expires_at"] <= now
                or row["key_id"] != self.cipher.key_id
                or not secrets.compare_digest(row["browser_hash"], token_digest(browser))
            ):
                raise OAuthRejected("invalid or consumed callback")
            verifier = self.cipher.decrypt(row["verifier_cipher"], str(row["id"])).decode()
            await session.execute(
                update(flows).where(flows.c.id == row["id"]).values(consumed_at=now)
            )
        # A failed exchange consumes the flow; no DB transaction spans provider I/O.
        identity = await self.provider.exchange(code, verifier)
        if identity.subject <= 0 or not identity.token or len(identity.token) > 16384:
            raise OAuthRejected("invalid provider identity")
        external = PHASE2_TABLES["external_identities"]
        grants = PHASE2_TABLES["github_user_grants"]
        async with transaction(self.factory) as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:subject, 0))"),
                {"subject": "github:" + str(identity.subject)},
            )
            principal = await session.scalar(
                select(external.c.principal_id).where(
                    external.c.provider == "github", external.c.subject == str(identity.subject)
                )
            )
            if principal is None:
                principal, org = uuid4(), uuid4()
                await session.execute(
                    insert(principals).values(
                        id=principal,
                        subject_key="github:" + str(identity.subject),
                        display_name=identity.display_name[:200] or "GitHub user",
                    )
                )
                await session.execute(
                    insert(organizations).values(
                        id=org, slug="personal-" + principal.hex, display_name="Personal workspace"
                    )
                )
                await session.execute(
                    insert(memberships).values(org_id=org, principal_id=principal, role="owner")
                )
                await session.execute(
                    insert(external).values(
                        provider="github", subject=str(identity.subject), principal_id=principal
                    )
                )
                await session.execute(insert(PHASE2_TABLES["org_work_limits"]).values(org_id=org))
            now = await database_now(session)
            if identity.expires_at.tzinfo is None or identity.expires_at <= now:
                raise OAuthRejected("provider grant expired")
            values = {
                "token_cipher": self.cipher.encrypt(identity.token.encode(), str(principal)),
                "key_id": self.cipher.key_id,
                "expires_at": identity.expires_at,
                "revoked_at": None,
                "last_verified_at": now,
            }
            await session.execute(
                pg_insert(grants)
                .values(principal_id=principal, **values)
                .on_conflict_do_update(
                    index_elements=[grants.c.principal_id],
                    set_={**values, "version": grants.c.version + 1},
                )
            )
        return UUID(str(principal))
