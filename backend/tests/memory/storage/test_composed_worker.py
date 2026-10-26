"""Attempt-local worker composition with real SQL and a synthetic provider."""

from app.contracts.investigation_jobs import InvestigationCreateRequestV2
from app.models.investigation_tables import investigations
from app.models.phase2_tables import PHASE2_TABLES
from app.models.tables import artifacts, source_snapshots
from app.security.authorization import require_repository
from app.services.investigation_jobs import InvestigationJobService
from app.services.investigation_runtime import build_investigation_operation
from app.services.investigation_tools import InvestigationToolService
from app.services.jobs import JobService
from app.services.model_call_results import SqlModelResultStore
from app.services.model_continuations import ContinuationCipher
from app.services.model_runtime import ConfiguredModelRuntime
from sqlalchemy import select
from tests.model_connections.storage.test_configured_gateway import configured


async def test_composed_worker_publishes_indeterminate_result(
    migration_factory, api_factory, broker_factory, tmp_path
):
    configured_gateway, args, client, calls = await configured(migration_factory, tmp_path)
    async with migration_factory() as session:
        ctx = await require_repository(
            session, args["org_id"], args["principal_id"], args["repository_id"], write=True
        )
        snapshot = await session.scalar(
            select(source_snapshots.c.id)
            .join(
                artifacts,
                (artifacts.c.org_id == source_snapshots.c.org_id)
                & (artifacts.c.id == source_snapshots.c.artifact_id),
            )
            .join(
                PHASE2_TABLES["object_records"],
                (PHASE2_TABLES["object_records"].c.org_id == artifacts.c.org_id)
                & (PHASE2_TABLES["object_records"].c.key == artifacts.c.storage_key)
                & (PHASE2_TABLES["object_records"].c.state == "available"),
            )
            .where(
                source_snapshots.c.org_id == ctx.org_id,
                source_snapshots.c.repository_id == args["repository_id"],
            )
        )
    commands = InvestigationJobService(migration_factory)
    created = await commands.create(
        ctx,
        InvestigationCreateRequestV2(
            repository_id=args["repository_id"],
            snapshot_id=snapshot,
            query="Inspect fixture without making unsupported claims",
            target_type="repository",
        ),
        request_key="composed-create",
    )
    started = await commands.start(ctx, created.id, request_key="composed-start")
    lease = await JobService(broker_factory).claim(ctx.org_id, started.job_id, 1, "composed-worker")
    assert lease is not None
    runtime = ConfiguredModelRuntime(
        configured_gateway.registry,
        configured_gateway.transports,
        broker_factory,
        admission_factory=api_factory,
        continuations=configured_gateway.continuations,
        disclosure_policies={(ctx.org_id, args["repository_id"]): args["disclosure_policy"]},
    )
    operation = build_investigation_operation(
        runtime=runtime,
        worker_factory=broker_factory,
        tools=InvestigationToolService(broker_factory),
        result_store=SqlModelResultStore(broker_factory, ContinuationCipher(bytes(range(32)))),
    )
    try:
        outcome = await operation.execute(lease)
        assert len(calls) == 1, (outcome.status, outcome.stop_reason, outcome.message)
        async with broker_factory() as session:
            row = (
                (
                    await session.execute(
                        select(investigations).where(
                            investigations.c.org_id == ctx.org_id, investigations.c.id == created.id
                        )
                    )
                )
                .mappings()
                .one()
            )
            assert row["status"] == "completed"
            assert row["verdict"] == "indeterminate"
            assert row["final_report_id"] is not None
    finally:
        await client.aclose()
