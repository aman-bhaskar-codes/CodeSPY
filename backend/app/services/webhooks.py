"""Authenticated revocations are monotonic; webhook data never grants access."""

import json
from datetime import timedelta

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.transaction import transaction
from app.domain.errors import DuplicateConflict
from app.models.phase2_tables import PHASE2_TABLES
from app.security.github import verify_webhook


class GitHubWebhookService:
    def __init__(self, factory: async_sessionmaker[AsyncSession], secret: bytes):
        if not secret:
            raise ValueError("explicit webhook secret required")
        self.factory, self.secret = factory, secret

    async def receive(self, delivery_id: str, event_type: str, signature: str, body: bytes) -> None:
        digest = verify_webhook(body, signature, self.secret)
        if not 1 <= len(delivery_id) <= 128 or event_type not in {
            "installation",
            "installation_repositories",
            "github_app_authorization",
        }:
            raise ValueError("unsupported webhook envelope")
        payload = json.loads(body)
        action = payload.get("action")
        if not isinstance(payload, dict):
            raise ValueError("invalid webhook body")
        if event_type == "github_app_authorization":
            subject = payload.get("sender", {}).get("id")
            if action != "revoked" or type(subject) is not int or subject <= 0:
                raise ValueError("invalid user revocation")
            await self._revoke_user(delivery_id, digest, subject)
            return
        installation = payload.get("installation", {}).get("id")
        if type(installation) is not int or installation <= 0:
            raise ValueError("invalid installation identity")
        repository_ids = payload.get("repositories_removed", [])
        if not isinstance(repository_ids, list) or len(repository_ids) > 5000:
            raise ValueError("unsupported repository capacity")
        removed = [item.get("id") for item in repository_ids if isinstance(item, dict)]
        if any(type(value) is not int or value <= 0 for value in removed):
            raise ValueError("invalid repository identity")
        receipts, bindings, limits = (
            PHASE2_TABLES[name]
            for name in ["webhook_receipts", "github_repository_bindings", "org_work_limits"]
        )
        async with transaction(self.factory) as s:
            await s.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:delivery,0))"),
                {"delivery": delivery_id},
            )
            existing = (
                (await s.execute(select(receipts).where(receipts.c.delivery_id == delivery_id)))
                .mappings()
                .one_or_none()
            )
            if existing:
                if existing["body_sha256"] != digest or existing["event_type"] != event_type:
                    raise DuplicateConflict("webhook delivery changed")
                return
            # Only deletion/suspension/removal can mutate authority. Older add/unsuspend
            # deliveries cannot reactivate a binding; live re-verification is required.
            query = select(bindings).where(bindings.c.installation_id == installation)
            revokes = event_type == "installation" and action in {"deleted", "suspend"}
            if event_type == "installation_repositories" and action == "removed":
                query = query.where(bindings.c.github_repository_id.in_(removed))
                revokes = True
            rows = (
                (await s.execute(query.order_by(bindings.c.org_id, bindings.c.repository_id)))
                .mappings()
                .all()
                if revokes
                else []
            )
            for org in sorted({row["org_id"] for row in rows}, key=str):
                await s.execute(
                    select(limits.c.org_id).where(limits.c.org_id == org).with_for_update()
                )
            for row in rows:
                await s.execute(
                    update(bindings)
                    .where(
                        bindings.c.org_id == row["org_id"],
                        bindings.c.repository_id == row["repository_id"],
                    )
                    .values(
                        state="suspended" if action == "suspend" else "revoked",
                        version=bindings.c.version + 1,
                        last_checked_at=func.clock_timestamp(),
                    )
                )
                await s.execute(
                    update(PHASE2_TABLES["object_records"])
                    .where(
                        PHASE2_TABLES["object_records"].c.org_id == row["org_id"],
                        PHASE2_TABLES["object_records"].c.repository_id == row["repository_id"],
                        PHASE2_TABLES["object_records"].c.kind.in_(["input", "stage"]),
                    )
                    .values(expires_at=func.clock_timestamp() + timedelta(seconds=15))
                )
            await s.execute(
                insert(receipts).values(
                    delivery_id=delivery_id,
                    event_type=event_type,
                    body_sha256=digest,
                    state="processed",
                    normalized_action={
                        "action": action
                        if action in {"deleted", "suspend", "removed"}
                        else "ignored"
                    },
                    processed_at=func.clock_timestamp(),
                )
            )

    async def _revoke_user(self, delivery_id: str, digest: str, subject: int) -> None:
        from app.models.tables import memberships

        receipts, identities, grants, limits, subs, objects = (
            PHASE2_TABLES[name]
            for name in [
                "webhook_receipts",
                "external_identities",
                "github_user_grants",
                "org_work_limits",
                "source_submissions",
                "object_records",
            ]
        )
        async with transaction(self.factory) as s:
            await s.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:delivery,0))"),
                {"delivery": delivery_id},
            )
            previous = (
                (await s.execute(select(receipts).where(receipts.c.delivery_id == delivery_id)))
                .mappings()
                .one_or_none()
            )
            if previous:
                if (
                    previous["body_sha256"] != digest
                    or previous["event_type"] != "github_app_authorization"
                ):
                    raise DuplicateConflict("webhook delivery changed")
                return
            principal = await s.scalar(
                select(identities.c.principal_id).where(
                    identities.c.provider == "github", identities.c.subject == str(subject)
                )
            )
            if principal is not None:
                orgs = (
                    await s.scalars(
                        select(memberships.c.org_id)
                        .where(memberships.c.principal_id == principal)
                        .order_by(memberships.c.org_id)
                    )
                ).all()
                for org in orgs:
                    await s.execute(
                        select(limits.c.org_id).where(limits.c.org_id == org).with_for_update()
                    )
                await s.execute(
                    update(grants)
                    .where(grants.c.principal_id == principal)
                    .values(revoked_at=func.clock_timestamp(), version=grants.c.version + 1)
                )
                inputs = select(subs.c.input_object_id).where(
                    subs.c.principal_id == principal, subs.c.input_object_id.is_not(None)
                )
                await s.execute(
                    update(objects)
                    .where(objects.c.id.in_(inputs), objects.c.kind == "input")
                    .values(expires_at=func.clock_timestamp() + timedelta(seconds=15))
                )
            await s.execute(
                insert(receipts).values(
                    delivery_id=delivery_id,
                    event_type="github_app_authorization",
                    body_sha256=digest,
                    state="processed",
                    normalized_action={"action": "revoked"},
                    processed_at=func.clock_timestamp(),
                )
            )
