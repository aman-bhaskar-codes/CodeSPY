"""Durable scoped encrypted result store and dispatch authority for model calls.

Provides crash-safe replay of settled model responses without duplicate network
invocations or duplicate charges, and linearizes dispatch permission with job authority.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import timedelta
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidTag
from sqlalchemy import delete, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.investigation_execution import ModelInvocationRefV1
from app.contracts.model import ModelResponseV1
from app.db.time import database_now
from app.domain.canonical_json import semantic_digest
from app.domain.scope import TenantContext
from app.models.investigation_execution_tables import model_call_results
from app.models.investigation_tables import investigations
from app.models.phase2_tables import jobs
from app.ports.investigation_execution import ModelDispatchAuthorityPort, ModelResultStorePort
from app.services.configured_model_gateway import ModelConnectionError
from app.services.model_continuations import ContinuationCipher

logger = logging.getLogger(__name__)

# Max usable payload: 512 KiB (524,288 bytes)
MAX_USABLE_PAYLOAD_BYTES = 512 * 1024


class SqlModelResultStore(ModelResultStorePort):
    """Encrypted PostgreSQL / SQLite storage for model call results."""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        cipher: ContinuationCipher,
    ) -> None:
        self.factory = factory
        self.cipher = cipher

    @staticmethod
    def _context(invocation: ModelInvocationRefV1) -> str:
        """Deterministic encryption context binding tenant, repo, investigation, call, profile."""
        return semantic_digest(
            [
                str(invocation.org_id),
                str(invocation.repository_id),
                str(invocation.principal_id),
                str(invocation.investigation_id),
                str(invocation.node_id),
                str(invocation.reservation_id),
                invocation.request_digest,
                invocation.profile_digest,
            ]
        )

    @staticmethod
    def _parameters(invocation: ModelInvocationRefV1) -> dict[str, object]:
        return {
            "org": invocation.org_id,
            "repo": invocation.repository_id,
            "actor": invocation.principal_id,
            "inv": invocation.investigation_id,
            "res": invocation.reservation_id,
            "call": invocation.node_id,
            "request": invocation.request_digest,
            "profile": invocation.profile_digest,
        }

    async def get(
        self,
        invocation: ModelInvocationRefV1,
        actor: TenantContext | None = None,
    ) -> ModelResponseV1 | None:
        """Retrieve a retained result for the exact invocation identity.

        Returns None if not found, expired, or digest mismatch.
        Raises ModelConnectionError if outcome was unusable/invalid.
        """
        if actor is not None and (
            actor.org_id != invocation.org_id or actor.principal_id != invocation.principal_id
        ):
            return None

        async with self.factory() as session:
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                row = (
                    (
                        await session.execute(
                            text(
                                "SELECT * FROM public.codespy_get_model_result("
                                ":org,:repo,:actor,:inv,:res,:call,:request,:profile)"
                            ),
                            self._parameters(invocation),
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
            else:
                stmt = select(model_call_results).where(
                    model_call_results.c.org_id == invocation.org_id,
                    model_call_results.c.reservation_id == invocation.reservation_id,
                )
                row = (await session.execute(stmt)).mappings().one_or_none()
            if row is None:
                return None

            # Verify request digest and profile revision match
            if (
                row["request_digest"] != invocation.request_digest
                or row["profile_revision"] != invocation.profile_digest
            ):
                return None

            # Verify unexpired using DB time
            now = await database_now(session)
            expires_at = row["expires_at"]
            if expires_at is not None:
                if expires_at.tzinfo is None and now.tzinfo is not None:
                    expires_at = expires_at.replace(tzinfo=now.tzinfo)
                elif expires_at.tzinfo is not None and now.tzinfo is None:
                    now = now.replace(tzinfo=expires_at.tzinfo)
                if expires_at <= now:
                    return None

            status = row["outcome_status"]
            if status in ("unusable", "invalid"):
                code = row["failure_code"] or "unusable_model_result"
                raise ModelConnectionError(code)

            if status != "usable":
                return None

            ciphertext = row["ciphertext"]
            if not ciphertext:
                return None

            if hashlib.sha256(ciphertext).hexdigest() != row["ciphertext_digest"]:
                return None
            context = self._context(invocation)
            try:
                plaintext = self.cipher.decrypt(ciphertext, context)
                data = json.loads(plaintext.decode("utf-8"))
                return ModelResponseV1.model_validate(data)
            except (InvalidTag, ValueError, KeyError):
                logger.warning(
                    "retained_model_result_decryption_failed",
                    extra={"reservation_id": str(invocation.reservation_id)},
                )
                return None

    async def put_known(
        self,
        session: AsyncSession,
        invocation: ModelInvocationRefV1,
        response: ModelResponseV1,
        *,
        failure_code: str | None = None,
    ) -> UUID:
        """Store an encrypted result atomically within the caller's settlement transaction.

        Returns the record ID. Exact replay returns the existing ID without mutation.
        """
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            existing = await session.scalar(
                text(
                    "SELECT public.codespy_model_result_identity("
                    ":org,:repo,:actor,:inv,:res,:call,:request,:profile)"
                ),
                self._parameters(invocation),
            )
        else:
            existing = (
                await session.execute(
                    select(model_call_results.c.id).where(
                        model_call_results.c.org_id == invocation.org_id,
                        model_call_results.c.reservation_id == invocation.reservation_id,
                    )
                )
            ).scalar_one_or_none()
        if existing is not None:
            return UUID(str(existing))

        payload_bytes = response.model_dump_json().encode("utf-8")
        now = await database_now(session)
        expires_at = now + timedelta(days=7)
        result_id = uuid4()

        if len(payload_bytes) > MAX_USABLE_PAYLOAD_BYTES or failure_code is not None:
            outcome_status = "unusable"
            ciphertext = None
            ciphertext_digest = None
            code = failure_code or "payload_too_large"
        else:
            outcome_status = "usable"
            context = self._context(invocation)
            ciphertext = self.cipher.encrypt(payload_bytes, context)
            ciphertext_digest = hashlib.sha256(ciphertext).hexdigest()
            code = None

        await session.execute(
            insert(model_call_results).values(
                org_id=invocation.org_id,
                id=result_id,
                reservation_id=invocation.reservation_id,
                logical_call_id=invocation.node_id,
                request_digest=invocation.request_digest,
                model_id=response.model_id,
                provider=response.provider,
                profile_revision=invocation.profile_digest,
                outcome_status=outcome_status,
                ciphertext=ciphertext,
                ciphertext_digest=ciphertext_digest,
                failure_code=code,
                created_at=now,
                expires_at=expires_at,
            )
        )
        return result_id

    async def cleanup_expired(self, session: AsyncSession, limit: int = 100) -> int:
        """Delete up to limit expired model call result records."""
        if not 1 <= limit <= 100:
            raise ValueError("invalid result retention limit")
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            return int(
                await session.scalar(
                    text("SELECT public.codespy_expire_model_results(:limit)"), {"limit": limit}
                )
                or 0
            )
        now = await database_now(session)
        subq = (
            select(model_call_results.c.id)
            .where(
                model_call_results.c.expires_at <= now,
            )
            .limit(limit)
        )
        expired_ids = (await session.execute(subq)).scalars().all()
        if not expired_ids:
            return 0
        await session.execute(
            delete(model_call_results).where(model_call_results.c.id.in_(expired_ids))
        )
        return len(expired_ids)


class SqlModelDispatchAuthority(ModelDispatchAuthorityPort):
    """Enforces pre-dispatch job authority inside the dispatch marker transaction."""

    async def assert_dispatch_allowed(
        self,
        session: AsyncSession,
        investigation_id: UUID,
        logical_call_id: UUID,
    ) -> None:
        """Verify current job/lease/cancellation before claiming the dispatch marker.

        Raises ModelConnectionError on cancelled, expired, or terminal state.
        """
        now = await database_now(session)

        # 1. Check investigation lifecycle state
        inv_row = (
            (
                await session.execute(
                    select(
                        investigations.c.status,
                        investigations.c.current_job_id,
                    ).where(investigations.c.id == investigation_id)
                )
            )
            .mappings()
            .one_or_none()
        )

        if inv_row is None:
            raise ModelConnectionError("investigation_not_found")

        if inv_row["status"] in ("cancelled", "failed", "completed"):
            raise ModelConnectionError(f"investigation_{inv_row['status']}")

        job_id = inv_row["current_job_id"]
        if job_id is not None:
            job_row = (
                (
                    await session.execute(
                        select(
                            jobs.c.state,
                            jobs.c.deadline,
                            jobs.c.worker_lease_expires_at,
                        ).where(jobs.c.id == job_id)
                    )
                )
                .mappings()
                .one_or_none()
            )

            if job_row is not None:
                state = job_row["state"]
                if state in ("cancelling", "cancelled", "failed", "succeeded"):
                    raise ModelConnectionError(f"job_{state}")

                deadline = job_row["deadline"]
                if deadline is not None:
                    if deadline.tzinfo is None and now.tzinfo is not None:
                        deadline = deadline.replace(tzinfo=now.tzinfo)
                    elif deadline.tzinfo is not None and now.tzinfo is None:
                        now_cmp = now.replace(tzinfo=deadline.tzinfo)
                    else:
                        now_cmp = now
                    if deadline <= now_cmp:
                        raise ModelConnectionError("job_deadline_exceeded")

                lease = job_row["worker_lease_expires_at"]
                if lease is not None:
                    if lease.tzinfo is None and now.tzinfo is not None:
                        lease = lease.replace(tzinfo=now.tzinfo)
                    elif lease.tzinfo is not None and now.tzinfo is None:
                        now_cmp = now.replace(tzinfo=lease.tzinfo)
                    else:
                        now_cmp = now
                    if lease <= now_cmp:
                        raise ModelConnectionError("job_lease_expired")
