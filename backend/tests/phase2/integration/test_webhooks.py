import hashlib
import hmac
import json
from datetime import UTC, datetime

import pytest
from app.domain.errors import DuplicateConflict
from app.models.phase2_tables import PHASE2_TABLES
from app.services.webhooks import GitHubWebhookService
from sqlalchemy import insert, select
from tests.phase2.integration.test_submission_ingestion import submission_scenario


async def test_authenticated_webhook_is_idempotent_and_never_reactivates(factory, tmp_path):
    _, ctx, request, _, _, _ = await submission_scenario(factory, tmp_path)
    bindings = PHASE2_TABLES["github_repository_bindings"]
    async with factory.begin() as s:
        await s.execute(
            insert(bindings).values(
                org_id=ctx.org_id,
                repository_id=request.repository_id,
                github_repository_id=11,
                installation_id=22,
                account_id=33,
                state="active",
                last_checked_at=datetime.now(UTC),
            )
        )
    service = GitHubWebhookService(factory, b"fixture-webhook-secret")

    async def deliver(delivery, action):
        body = json.dumps({"action": action, "installation": {"id": 22}}).encode()
        signature = (
            "sha256=" + hmac.new(b"fixture-webhook-secret", body, hashlib.sha256).hexdigest()
        )
        await service.receive(delivery, "installation", signature, body)

    await deliver("one", "deleted")
    await deliver("one", "deleted")
    with pytest.raises(DuplicateConflict):
        await deliver("one", "unsuspend")
    await deliver("older-add", "unsuspend")
    async with factory() as s:
        assert (
            await s.scalar(select(bindings.c.state).where(bindings.c.org_id == ctx.org_id))
            == "revoked"
        )


async def test_user_authorization_revocation_revokes_encrypted_grant(factory, tmp_path):
    from datetime import timedelta

    _, ctx, _, _, _, _ = await submission_scenario(factory, tmp_path)
    async with factory.begin() as s:
        await s.execute(
            insert(PHASE2_TABLES["external_identities"]).values(
                provider="github", subject="42", principal_id=ctx.principal_id
            )
        )
        await s.execute(
            insert(PHASE2_TABLES["github_user_grants"]).values(
                principal_id=ctx.principal_id,
                token_cipher=b"fixture",
                key_id="fixture",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                last_verified_at=datetime.now(UTC),
            )
        )
    body = json.dumps({"action": "revoked", "sender": {"id": 42}}).encode()
    signature = "sha256=" + hmac.new(b"fixture-webhook-secret", body, hashlib.sha256).hexdigest()
    service = GitHubWebhookService(factory, b"fixture-webhook-secret")
    await service.receive("user-revoked", "github_app_authorization", signature, body)
    await service.receive("user-revoked", "github_app_authorization", signature, body)
    async with factory() as s:
        row = (await s.execute(select(PHASE2_TABLES["github_user_grants"]))).mappings().one()
        assert row["revoked_at"] is not None and row["version"] == 1
