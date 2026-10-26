"""Database-time authority around short model investigation transactions."""

import pytest
from app.contracts.investigation import InvestigationCreateRequestV1
from app.models.investigation_tables import investigations
from app.security.authorization import require_repository
from app.services.investigation_checkpointer import FencedCheckpointer, StaleWorkerFencingError
from app.services.transactional_investigation_checkpointer import (
    TransactionalInvestigationCheckpointer,
)
from sqlalchemy import select, text

from tests.model_connections.storage.test_configured_gateway import configured


async def test_expired_worker_cannot_publish_after_model_call(migration_factory, tmp_path):
    _gateway, args, client, _calls = await configured(migration_factory, tmp_path)
    try:
        async with migration_factory() as s, s.begin():
            actor = await require_repository(
                s, args["org_id"], args["principal_id"], args["repository_id"], write=True
            )
            cp = FencedCheckpointer(s)
            summary = await cp.create_investigation(
                args["org_id"],
                args["principal_id"],
                InvestigationCreateRequestV1(
                    repository_id=args["repository_id"], query="Synthetic authority fixture"
                ),
            )
            fence = await cp.acquire_lease(args["org_id"], summary.id, "worker-fixture")
        cp_runtime = TransactionalInvestigationCheckpointer(
            migration_factory, actor, args["repository_id"], summary.id, "worker-fixture", fence
        )
        async with migration_factory() as s, s.begin():
            await s.execute(
                text(
                    "UPDATE investigations SET worker_lease_expires_at="
                    "clock_timestamp()-interval '1 second' WHERE id=:id"
                ),
                {"id": summary.id},
            )
        with pytest.raises(StaleWorkerFencingError):
            await cp_runtime.complete_investigation(
                args["org_id"], summary.id, "worker-fixture", fence, "benign"
            )
        async with migration_factory() as s:
            assert (
                await s.scalar(
                    select(investigations.c.status).where(investigations.c.id == summary.id)
                )
                == "running"
            )
    finally:
        await client.aclose()
