"""Service authority regressions on disposable SQLite; not PostgreSQL guard qualification."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from app.contracts.triage import TriageTransitionV1
from app.domain.errors import ScopeViolation
from app.domain.scope import OrgRole, TenantContext
from app.models.phase2_tables import PHASE2_TABLES, md
from app.models.scan_tables import finding_occurrences, finding_triage_events, scan_reports
from app.models.tables import memberships, repositories, source_files, source_snapshots
from app.services.feature_trace import FeatureTraceService
from app.services.findings_query import FindingsQueryService
from app.services.triage import TriageService
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


@dataclass(frozen=True)
class AuthorityFixture:
    factory: async_sessionmaker[AsyncSession]
    ctx: TenantContext
    repository_id: UUID
    foreign_repository_id: UUID
    snapshot_id: UUID
    foreign_snapshot_id: UUID
    run_id: UUID
    finding_id: UUID


@pytest_asyncio.fixture
async def authority(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AuthorityFixture]:
    # Clone metadata: adapting SQL dialects must never mutate production constraints/types.
    metadata = sa.MetaData()
    for table in md.tables.values():
        clone = table.to_metadata(metadata)
        for constraint in clone.constraints:
            if isinstance(constraint, sa.CheckConstraint):
                constraint.ddl_if(dialect="postgresql")
        for column in clone.columns:
            if isinstance(column.type, JSONB):
                column.type = sa.JSON()
            elif isinstance(column.type, sa.Text) and column.type.collation:
                column.type = sa.Text()
            if column.server_default and "::jsonb" in str(column.server_default.arg):
                column.server_default = sa.DefaultClause(sa.text("'{}'"))

    names = [
        "memberships",
        "sessions",
        "repositories",
        "repository_grants",
        "source_snapshots",
        "source_files",
        "scan_reports",
        "finding_occurrences",
        "finding_support",
        "verification_decisions",
        "finding_triage_events",
        "finding_triage_projections",
        "cpg_nodes",
        "cpg_edges",
        "analysis_runs",
        "structural_reports",
    ]
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: metadata.create_all(sync, tables=[metadata.tables[n] for n in names])
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    org_id, principal_id = uuid4(), uuid4()
    repository_id, foreign_repository_id = uuid4(), uuid4()
    snapshot_id, foreign_snapshot_id, run_id, finding_id = uuid4(), uuid4(), uuid4(), uuid4()
    now = datetime.now(UTC)

    async def fixture_now(_session: AsyncSession) -> datetime:
        return now

    monkeypatch.setattr("app.services.triage.database_now", fixture_now)
    async with factory() as session, session.begin():
        await session.execute(
            memberships.insert().values(org_id=org_id, principal_id=principal_id, role="member")
        )
        await session.execute(
            repositories.insert(),
            [
                {
                    "org_id": org_id,
                    "id": repo,
                    "repository_key": str(repo),
                    "display_name": "Owned fixture",
                    "source_kind": "archive",
                }
                for repo in (repository_id, foreign_repository_id)
            ],
        )
        await session.execute(
            PHASE2_TABLES["repository_grants"]
            .insert()
            .values(
                org_id=org_id,
                repository_id=repository_id,
                principal_id=principal_id,
                permission="write",
            )
        )
        for repo, snap in (
            (repository_id, snapshot_id),
            (foreign_repository_id, foreign_snapshot_id),
        ):
            await session.execute(
                source_snapshots.insert().values(
                    org_id=org_id,
                    id=snap,
                    repository_id=repo,
                    artifact_id=uuid4(),
                    admission_config_hash="a" * 64,
                    identity_hash=str(snap),
                    manifest_hash="b" * 64,
                    state="sealed",
                    sealed_at=now,
                )
            )
            file_id = uuid4()
            await session.execute(
                source_files.insert().values(
                    org_id=org_id,
                    snapshot_id=snap,
                    id=file_id,
                    path="src/example.py",
                    sha256="c" * 64,
                    size_bytes=10,
                )
            )
            await session.execute(
                scan_reports.insert().values(
                    org_id=org_id,
                    repository_id=repo,
                    snapshot_id=snap,
                    run_id=run_id,
                    spec_id=uuid4(),
                    config_hash="d" * 64,
                    draft_json={},
                    canonical_text="{}",
                    draft_digest="e" * 64,
                    batch_hash="f" * 64,
                    coverage="complete",
                    omitted_findings=0,
                )
            )
            await session.execute(
                finding_occurrences.insert().values(
                    org_id=org_id,
                    snapshot_id=snap,
                    run_id=run_id,
                    id=finding_id,
                    file_id=file_id,
                    observation_key="synthetic",
                    category="source_pattern",
                    rule_id="fixture.rule",
                    payload_json={"title": "Fixture finding", "severity": "high"},
                    canonical_text="{}",
                    payload_digest="a" * 64,
                )
            )
    try:
        yield AuthorityFixture(
            factory,
            TenantContext(org_id=org_id, principal_id=principal_id, role=OrgRole.MEMBER),
            repository_id,
            foreign_repository_id,
            snapshot_id,
            foreign_snapshot_id,
            run_id,
            finding_id,
        )
    finally:
        await engine.dispose()


async def call_service(fixture: AuthorityFixture, operation: str, snapshot_id: UUID) -> object:
    ctx, repo = fixture.ctx, fixture.repository_id
    reads = FindingsQueryService(fixture.factory)
    triage = TriageService(fixture.factory)
    if operation == "summary":
        return await reads.get_summary(ctx, repo, snapshot_id, fixture.run_id)
    if operation == "list":
        return await reads.list_findings(ctx, repo, snapshot_id, fixture.run_id)
    if operation == "detail":
        return await reads.get_finding_detail(ctx, repo, snapshot_id, fixture.finding_id)
    if operation == "record":
        return await triage.get_record(ctx, repo, snapshot_id, fixture.finding_id)
    if operation == "history":
        return await triage.get_history(ctx, repo, snapshot_id, fixture.finding_id)
    if operation == "transition":
        return await triage.transition(
            ctx,
            repo,
            snapshot_id,
            fixture.finding_id,
            TriageTransitionV1(state="confirmed", expected_version=0),
        )
    return await FeatureTraceService(fixture.factory).trace(
        ctx, repo, snapshot_id, fixture.finding_id
    )


OPERATIONS = ["summary", "list", "detail", "record", "history", "transition", "trace"]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OPERATIONS)
async def test_authorized_finding_services_remain_available(
    authority: AuthorityFixture, operation: str
) -> None:
    assert await call_service(authority, operation, authority.snapshot_id) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize(
    "attack", ["foreign_snapshot", "revoked_grant", "removed_membership", "unsealed_snapshot"]
)
async def test_finding_services_recheck_authority(
    authority: AuthorityFixture, operation: str, attack: str
) -> None:
    target_snapshot = authority.snapshot_id
    async with authority.factory() as session, session.begin():
        if attack == "foreign_snapshot":
            target_snapshot = authority.foreign_snapshot_id
        elif attack == "revoked_grant":
            grants = PHASE2_TABLES["repository_grants"]
            await session.execute(sa.update(grants).values(revoked_at=datetime.now(UTC)))
        elif attack == "removed_membership":
            await session.execute(sa.delete(memberships))
        else:
            await session.execute(
                sa.update(source_snapshots)
                .where(source_snapshots.c.id == target_snapshot)
                .values(state="building", sealed_at=None, manifest_hash=None)
            )
    with pytest.raises((ScopeViolation, NoResultFound)):
        await call_service(authority, operation, target_snapshot)
    async with authority.factory() as session:
        assert (
            await session.scalar(sa.select(sa.func.count()).select_from(finding_triage_events)) == 0
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["read_grant", "viewer_role"])
async def test_triage_rechecks_write_authority_despite_stale_context(
    authority: AuthorityFixture, change: str
) -> None:
    async with authority.factory() as session, session.begin():
        if change == "read_grant":
            await session.execute(
                sa.update(PHASE2_TABLES["repository_grants"]).values(permission="read")
            )
        else:
            await session.execute(sa.update(memberships).values(role="viewer"))
    with pytest.raises(ScopeViolation):
        await call_service(authority, "transition", authority.snapshot_id)
    async with authority.factory() as session:
        assert (
            await session.scalar(sa.select(sa.func.count()).select_from(finding_triage_events)) == 0
        )
