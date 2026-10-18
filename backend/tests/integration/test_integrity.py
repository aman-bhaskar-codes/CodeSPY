from datetime import UTC, datetime
from uuid import uuid4

import pytest
from app.contracts.evidence import EvidenceRef
from app.db.transaction import transaction
from app.domain.errors import DuplicateConflict, InvalidTransition, ScopeViolation
from app.domain.scope import OrgRole
from app.models.tables import (
    analysis_runs,
    cpg_nodes,
    memberships,
    source_files,
    source_snapshots,
)
from app.repositories.evidence_repository import EvidenceRepository
from app.repositories.graph_repository import GraphRepository
from app.repositories.identity_repository import IdentityRepository
from sqlalchemy import insert, text, update
from sqlalchemy.exc import DBAPIError
from tests.support.fixtures import batch_for, scenario


async def reject(factory, stmt):
    with pytest.raises((InvalidTransition, ScopeViolation, DuplicateConflict)):
        async with transaction(factory) as s:
            await s.execute(stmt)


async def test_sealed_source_and_run_guards(factory):
    service, ctx, snapshot, run, files = await scenario(factory)
    await reject(
        factory,
        update(source_files)
        .where(source_files.c.snapshot_id == snapshot.id)
        .values(size_bytes=101),
    )
    await reject(
        factory,
        insert(source_files).values(
            org_id=ctx.org_id,
            snapshot_id=snapshot.id,
            id=uuid4(),
            path="late",
            sha256="a" * 64,
            size_bytes=0,
        ),
    )
    await reject(
        factory,
        update(source_snapshots)
        .where(source_snapshots.c.id == snapshot.id)
        .values(source_revision="changed"),
    )
    await reject(
        factory,
        update(analysis_runs)
        .where(analysis_runs.c.id == run.id)
        .values(
            config={"changed": True},
            status="cancelled",
            write_version=2,
            finished_at=datetime.now(UTC),
        ),
    )
    bad = batch_for(run.ref, files)
    bad = bad.model_copy(
        update={
            "nodes": [
                bad.nodes[0].model_copy(
                    update={"range": bad.nodes[0].range.model_copy(update={"end_byte": 101})}
                )
            ]
        }
    )
    with pytest.raises(InvalidTransition):
        await service.persist_result(ctx, run.ref, bad, 1)


async def test_same_local_ids_across_runs_and_tenants(factory):
    service, ctx, _snapshot, run, files = await scenario(factory)
    b = batch_for(run.ref, files)
    await service.persist_result(ctx, run.ref, b, 1)
    r2 = await service.create_run(
        ctx, run.request.model_copy(update={"request_key": uuid4().hex, "config": {"version": 2}})
    )
    await service.start_run(ctx, r2.ref, 0)
    b2 = batch_for(r2.ref, files)
    b2 = b2.model_copy(
        update={
            "nodes": b.nodes,
            "edges": [
                b2.edges[0].model_copy(
                    update={
                        "source_local_id": b.nodes[0].local_id,
                        "target_local_id": b.nodes[1].local_id,
                    }
                )
            ],
        }
    )
    await service.persist_result(ctx, r2.ref, b2, 1)
    s2, c2, _snap2, r3, f2 = await scenario(factory)
    b3 = batch_for(r3.ref, f2)
    nodes = [b3.nodes[i].model_copy(update={"local_id": b.nodes[i].local_id}) for i in range(2)]
    b3 = b3.model_copy(
        update={
            "nodes": nodes,
            "edges": [
                b3.edges[0].model_copy(
                    update={
                        "source_local_id": nodes[0].local_id,
                        "target_local_id": nodes[1].local_id,
                    }
                )
            ],
        }
    )
    await s2.persist_result(c2, r3.ref, b3, 1)
    assert sorted(
        (await service.read_published_graph(ctx, r2.ref)).nodes, key=lambda n: str(n.local_id)
    ) == sorted(b.nodes, key=lambda n: str(n.local_id))
    with pytest.raises(ScopeViolation):
        await service.read_published_graph(c2, run.ref)
    async with transaction(factory) as s:
        evidence = await EvidenceRepository(s).read_evidence(
            ctx, EvidenceRef(scope=run.ref, evidence_id=b.evidence[0].id)
        )
        assert evidence.digest == b.evidence[0].semantic_hash()


async def test_foreign_node_file_and_evidence_rejected(factory):
    svc, ctx, _snap, run, files = await scenario(factory)
    _, _c2, _snap2, _run2, files2 = await scenario(factory)
    b = batch_for(run.ref, files)
    for changed in [
        b.model_copy(update={"nodes": [b.nodes[0].model_copy(update={"file_id": files2[0]})]}),
        b.model_copy(
            update={"edges": [b.edges[0].model_copy(update={"support_evidence_id": uuid4()})]}
        ),
    ]:
        with pytest.raises((ScopeViolation, InvalidTransition)):
            await svc.persist_result(ctx, run.ref, changed, 1)


async def test_role_membership_and_bounds(factory):
    svc, ctx, _snap, run, _files = await scenario(factory)
    async with transaction(factory) as s:
        await s.execute(
            update(memberships).where(memberships.c.org_id == ctx.org_id).values(role="viewer")
        )
    viewer = ctx.model_copy(update={"role": OrgRole.VIEWER})
    with pytest.raises(ScopeViolation):
        await svc.cancel_run(viewer, run.ref, 1)
    with pytest.raises(ScopeViolation):
        await svc.cancel_run(ctx, run.ref, 1)
    with pytest.raises(ValueError):
        await svc.read_published_graph(viewer, run.ref, 201)


async def test_publication_immutable_and_neighbors(factory):
    svc, ctx, snap, run, files = await scenario(factory)
    b = batch_for(run.ref, files)
    await svc.persist_result(ctx, run.ref, b, 1)
    async with transaction(factory) as s:
        page = await GraphRepository(s).neighbors(ctx, run.ref, b.nodes[0].local_id)
        assert page.edges == b.edges
    await reject(
        factory,
        insert(cpg_nodes).values(
            org_id=ctx.org_id,
            snapshot_id=snap.id,
            run_id=run.id,
            **{
                **b.nodes[0].model_dump(exclude={"range"}),
                "local_id": uuid4(),
                **b.nodes[0].range.model_dump(),
            },
        ),
    )
    # UPDATE/DELETE and TRUNCATE privileges must be denied, independent of DTOs.
    for stmt in [
        "UPDATE evidence_records SET payload='{}'",
        "TRUNCATE cpg_nodes",
        "ALTER TABLE cpg_nodes DISABLE TRIGGER ALL",
        "CREATE TABLE forbidden(id int)",
    ]:
        with pytest.raises(DBAPIError):
            async with factory() as s, s.begin():
                await s.execute(text(stmt))


async def test_request_conflict_and_partial(factory):
    svc, ctx, _snap, run, files = await scenario(factory)
    with pytest.raises(DuplicateConflict):
        await svc.create_run(ctx, run.request.model_copy(update={"config": {"changed": True}}))
    b = batch_for(run.ref, files)
    b = b.model_copy(
        update={
            "disposition": "partial",
            "coverage": b.coverage.model_copy(
                update={"status": "partial", "processed_count": 1, "omitted_count": 1}
            ),
        }
    )
    result = await svc.persist_result(ctx, run.ref, b, 1)
    assert result.status == "partial" and result.limitations


async def test_shared_transaction_rollback_and_timeouts(factory, engine):
    async with engine.connect() as c:
        assert await c.scalar(text("SHOW statement_timeout")) == "10s"
        assert await c.scalar(text("SHOW lock_timeout")) == "2s"
    slug = "rollback-" + uuid4().hex[:12]
    with pytest.raises(RuntimeError):
        async with transaction(factory) as s:
            await IdentityRepository(s).create_fixture(slug)
            raise RuntimeError("fixture failure")
    async with transaction(factory) as s:
        assert (
            await s.scalar(
                text("SELECT count(*) FROM organizations WHERE slug=:slug"), {"slug": slug}
            )
            == 0
        )


async def test_database_guard_body_not_only_permissions(factory, db_values):
    import psycopg
    from scripts import phase1_db as db

    _svc, _ctx, snap, run, _files = await scenario(factory)
    with db.connect(
        db_values,
        db_values["CODESPY_TEST_DATABASE"],
        user="p1_migration",
        password=db_values["CODESPY_TEST_MIGRATION_PASSWORD"],
    ) as c:
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute("UPDATE source_files SET size_bytes=101 WHERE snapshot_id=%s", (snap.id,))
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute(
                "UPDATE analysis_runs SET status='completed',write_version=2,"
                "finished_at=CURRENT_TIMESTAMP,result_hash=%s,coverage=%s::jsonb WHERE id=%s",
                ("a" * 64, "{}", run.id),
            )
        roles = c.execute(
            "SELECT rolname,rolsuper,rolcreatedb,rolcreaterole,rolbypassrls "
            "FROM pg_roles WHERE rolname='p1_app'"
        ).fetchone()
        assert roles == ("p1_app", False, False, False, False)


async def test_statement_timeout_and_task_cancellation(factory, engine):
    import asyncio

    from app.domain.errors import DatabaseUnavailable
    from app.repositories.identity_repository import IdentityRepository

    with pytest.raises(DatabaseUnavailable) as error:
        async with transaction(factory) as s:
            await s.execute(text("SET LOCAL statement_timeout='20ms'"))
            await s.execute(text("SELECT pg_sleep(1)"))
    assert error.value.sqlstate == "57014"
    slug = "cancel-" + uuid4().hex[:12]
    with pytest.raises(asyncio.CancelledError):
        async with transaction(factory) as s:
            await IdentityRepository(s).create_fixture(slug)
            raise asyncio.CancelledError()
    async with transaction(factory) as s:
        assert (
            await s.scalar(
                text("SELECT count(*) FROM organizations WHERE slug=:slug"), {"slug": slug}
            )
            == 0
        )


async def test_publication_cannot_omit_preexisting_rows(factory):
    from app.domain.errors import DuplicateConflict
    from app.repositories.graph_repository import GraphRepository

    svc, ctx, _snap, run, files = await scenario(factory)
    b = batch_for(run.ref, files)
    async with transaction(factory) as s:
        await GraphRepository(s).insert_batch(ctx, run.ref, b.nodes, [])
    with pytest.raises(DuplicateConflict):
        await svc.persist_result(ctx, run.ref, b.model_copy(update={"nodes": [], "edges": []}), 1)


async def test_null_coverage_status_cannot_publish(factory):
    _svc, _ctx, _snap, run, _files = await scenario(factory)
    with pytest.raises(InvalidTransition):
        async with transaction(factory) as s:
            await s.execute(
                update(analysis_runs)
                .where(analysis_runs.c.id == run.id)
                .values(
                    status="completed",
                    write_version=2,
                    result_hash="a" * 64,
                    coverage={"status": None},
                    finished_at=datetime.now(UTC),
                )
            )


async def test_extension_provisioning_is_idempotent(db_values):
    from scripts import phase1_db as db

    first = db.provision_extensions(db_values, db_values["CODESPY_TEST_DATABASE"])
    second = db.provision_extensions(db_values, db_values["CODESPY_TEST_DATABASE"])
    assert first == second
    assert {"vector", "pg_trgm", "citext", "uuid-ossp"} <= first.keys()
