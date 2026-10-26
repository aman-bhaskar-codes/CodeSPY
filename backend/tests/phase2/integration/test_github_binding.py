from datetime import UTC, datetime, timedelta

import pytest
from app.domain.errors import DuplicateConflict
from app.models.phase2_tables import PHASE2_TABLES
from app.models.tables import repositories
from app.services.github_acquisition import GitHubAcquisitionService, RepositoryAccess
from sqlalchemy import insert, update

from tests.phase2.integration.test_submission_ingestion import submission_scenario


async def test_binding_needs_verified_user_app_intersection_and_owner(factory, tmp_path):
    _, ctx, request, _, _, store = await submission_scenario(factory, tmp_path)
    async with factory.begin() as s:
        await s.execute(
            update(repositories)
            .where(repositories.c.id == request.repository_id)
            .values(source_kind="github")
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

    class Provider:
        async def verify_access(self, principal, repository_id, installation_id):
            return RepositoryAccess(repository_id, installation_id, 33)

    service = GitHubAcquisitionService(factory, store, Provider())
    assert (await service.bind(ctx, request.repository_id, 11, 22)).repository_id == 11
    with pytest.raises(DuplicateConflict):
        await service.bind(ctx, request.repository_id, 12, 22)
