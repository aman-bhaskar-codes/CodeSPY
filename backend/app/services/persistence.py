"""Local persistence boundary; no target execution or external services."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.persistence import (
    GraphPage,
    RepositoryCreate,
    RepositoryRecord,
    ResultBatchV1,
    RunCreate,
    RunRecord,
    SnapshotCreate,
    SnapshotRecord,
    SourceFileCreate,
)
from app.db.transaction import transaction
from app.domain.canonical_json import semantic_digest
from app.domain.errors import DuplicateConflict, InvalidTransition, ScopeViolation, StaleWrite
from app.domain.scope import RunRef, TenantContext
from app.models.tables import (
    analysis_runs,
    artifacts,
    cpg_edges,
    cpg_nodes,
    evidence_derivations,
    evidence_records,
    repositories,
    source_files,
    source_snapshots,
)
from app.repositories.analysis_repository import AnalysisRepository, run_filter
from app.repositories.evidence_repository import EvidenceRepository
from app.repositories.graph_repository import GraphRepository, edge_dto, node_dto, scope_filter
from app.repositories.identity_repository import authorize
from app.repositories.repository_repository import RepositoryRepository


class PersistenceService:
    def __init__(self, factory: async_sessionmaker[AsyncSession]):
        self.factory = factory

    async def create_repository(
        self, ctx: TenantContext, data: RepositoryCreate
    ) -> RepositoryRecord:
        async with transaction(self.factory) as s:
            await authorize(s, ctx, True)
            await s.execute(select(func.pg_advisory_xact_lock(ctx.org_id.int % (2**63))))
            result = await RepositoryRepository(s).create(ctx, data)
        return result

    async def create_snapshot(
        self, ctx: TenantContext, data: SnapshotCreate, files: list[SourceFileCreate]
    ) -> SnapshotRecord:
        async with transaction(self.factory) as s:
            result = await self._create_snapshot_in_session(s, ctx, data, files)
        return result

    async def _create_snapshot_in_session(
        self,
        s: AsyncSession,
        ctx: TenantContext,
        data: SnapshotCreate,
        files: list[SourceFileCreate],
    ) -> SnapshotRecord:
        if (
            len(files) > 50000
            or len({f.path for f in files}) != len(files)
            or len({f.id for f in files}) != len(files)
        ):
            raise ValueError("invalid bounded manifest")
        manifest = [
            f.model_dump(exclude={"id"}) for f in sorted(files, key=lambda f: f.path.encode())
        ]
        manifest_hash = semantic_digest(manifest)
        identity = semantic_digest(
            {
                "org_id": ctx.org_id,
                "repository_id": data.repository_id,
                "artifact_sha256": data.artifact.sha256,
                "source_revision": data.source_revision,
                "admission_config_hash": data.admission_config_hash,
            }
        )
        await RepositoryRepository(s).get(ctx, data.repository_id)
        await authorize(s, ctx, True)
        await s.execute(
            select(repositories)
            .where(repositories.c.org_id == ctx.org_id, repositories.c.id == data.repository_id)
            .with_for_update()
        )
        old = (
            (
                await s.execute(
                    select(source_snapshots).where(
                        source_snapshots.c.org_id == ctx.org_id,
                        source_snapshots.c.repository_id == data.repository_id,
                        source_snapshots.c.identity_hash == identity,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if old:
            artifact = (
                (
                    await s.execute(
                        select(artifacts).where(
                            artifacts.c.org_id == ctx.org_id,
                            artifacts.c.id == old["artifact_id"],
                        )
                    )
                )
                .mappings()
                .one()
            )
            if old["manifest_hash"] != manifest_hash or any(
                artifact[k] != v for k, v in data.artifact.model_dump().items()
            ):
                raise DuplicateConflict("snapshot identity has conflicting source metadata")
            result = SnapshotRecord(**old, file_count=len(files))
        else:
            snapshot_id, artifact_id = uuid4(), uuid4()
            await s.execute(
                insert(artifacts).values(
                    org_id=ctx.org_id,
                    id=artifact_id,
                    repository_id=data.repository_id,
                    **data.artifact.model_dump(),
                )
            )
            await s.execute(
                insert(source_snapshots).values(
                    org_id=ctx.org_id,
                    id=snapshot_id,
                    repository_id=data.repository_id,
                    artifact_id=artifact_id,
                    source_revision=data.source_revision,
                    admission_config_hash=data.admission_config_hash,
                    identity_hash=identity,
                )
            )
            if files:
                await s.execute(
                    insert(source_files),
                    [
                        {"org_id": ctx.org_id, "snapshot_id": snapshot_id, **f.model_dump()}
                        for f in files
                    ],
                )
            row = (
                (
                    await s.execute(
                        update(source_snapshots)
                        .where(
                            source_snapshots.c.org_id == ctx.org_id,
                            source_snapshots.c.id == snapshot_id,
                        )
                        .values(
                            state="sealed",
                            manifest_hash=manifest_hash,
                            sealed_at=datetime.now(UTC),
                        )
                        .returning(source_snapshots)
                    )
                )
                .mappings()
                .one()
            )
            result = SnapshotRecord(**row, file_count=len(files))
        return result

    async def create_run(self, ctx: TenantContext, data: RunCreate) -> RunRecord:
        async with transaction(self.factory) as s:
            await authorize(s, ctx, True)
            await s.execute(select(func.pg_advisory_xact_lock(ctx.org_id.int % (2**63))))
            result = await self._create_run_in_session(s, ctx, data)
        return result

    async def _create_run_in_session(
        self, s: AsyncSession, ctx: TenantContext, data: RunCreate
    ) -> RunRecord:
        return await AnalysisRepository(s).create_run(ctx, data)

    async def _resolve_snapshot_files_in_session(
        self,
        s: AsyncSession,
        ctx: TenantContext,
        snapshot_id: UUID,
        expected_files: list[SourceFileCreate],
    ) -> tuple[SourceFileCreate, ...]:
        rows = (
            (
                await s.execute(
                    select(source_files)
                    .where(
                        source_files.c.org_id == ctx.org_id,
                        source_files.c.snapshot_id == snapshot_id,
                    )
                    .order_by(source_files.c.path)
                )
            )
            .mappings()
            .all()
        )
        stored = tuple(
            SourceFileCreate(**{k: row[k] for k in SourceFileCreate.model_fields}) for row in rows
        )
        expected = sorted(
            (f.model_dump(exclude={"id"}) for f in expected_files), key=lambda f: f["path"].encode()
        )
        stored = tuple(sorted(stored, key=lambda f: f.path.encode()))
        actual = [f.model_dump(exclude={"id"}) for f in stored]
        if actual != expected:
            raise DuplicateConflict("persisted file metadata differs from admission")
        return stored

    async def _transition(
        self,
        ctx: TenantContext,
        ref: RunRef,
        expected_version: int,
        status: str,
        failure_code: str | None = None,
    ) -> RunRecord:
        async with transaction(self.factory) as s:
            result = await self._transition_in_session(
                s, ctx, ref, expected_version, status, failure_code
            )
        return result

    async def _transition_in_session(
        self,
        s: AsyncSession,
        ctx: TenantContext,
        ref: RunRef,
        expected_version: int,
        status: str,
        failure_code: str | None = None,
    ) -> RunRecord:
        old = await AnalysisRepository(s).get(ctx, ref, lock=True)
        if old.write_version != expected_version:
            raise StaleWrite("lifecycle version changed")
        if not (
            (old.status == "queued" and status in {"running", "cancelled"})
            or (old.status == "running" and status in {"cancelled", "failed"})
        ):
            raise InvalidTransition("run cannot transition")
        values = {"status": status, "write_version": old.write_version + 1}
        if status == "running":
            values["started_at"] = datetime.now(UTC)
        else:
            values["finished_at"] = datetime.now(UTC)
        if status == "failed":
            values["failure_code"] = failure_code
        row = (
            (
                await s.execute(
                    update(analysis_runs)
                    .where(*run_filter(ref))
                    .values(**values)
                    .returning(analysis_runs)
                )
            )
            .mappings()
            .one()
        )
        result = RunRecord(**{k: row[k] for k in RunRecord.model_fields})
        return result

    async def start_run(self, ctx: TenantContext, ref: RunRef, expected_version: int) -> RunRecord:
        return await self._transition(ctx, ref, expected_version, "running")

    async def cancel_run(self, ctx: TenantContext, ref: RunRef, expected_version: int) -> RunRecord:
        return await self._transition(ctx, ref, expected_version, "cancelled")

    async def fail_run(
        self, ctx: TenantContext, ref: RunRef, expected_version: int, failure_code: str
    ) -> RunRecord:
        if (
            not failure_code
            or len(failure_code.encode()) > 128
            or not all(c.isalnum() or c in "_-" for c in failure_code)
        ):
            raise ValueError("sanitized failure code required")
        return await self._transition(ctx, ref, expected_version, "failed", failure_code)

    async def persist_result(
        self, ctx: TenantContext, ref: RunRef, batch: ResultBatchV1, expected_version: int
    ) -> RunRecord:
        async with transaction(self.factory) as s:
            result = await self._persist_result_in_session(s, ctx, ref, batch, expected_version)
        return result

    async def _persist_result_in_session(
        self,
        s: AsyncSession,
        ctx: TenantContext,
        ref: RunRef,
        batch: ResultBatchV1,
        expected_version: int,
    ) -> RunRecord:
        # Revalidate model_copy results and nested mutable data before persistence.
        batch = ResultBatchV1.model_validate(batch.model_dump())
        if ctx.org_id != ref.org_id or batch.scope != ref:
            raise ScopeViolation("result scope differs")
        result_hash = batch.semantic_hash()
        old = await AnalysisRepository(s).get(ctx, ref, lock=True)
        if old.status in {"completed", "partial"}:
            if old.result_hash == result_hash and old.status == batch.disposition:
                return old
            raise DuplicateConflict("published result differs")
        if old.write_version != expected_version:
            raise StaleWrite("lifecycle version changed")
        if old.status != "running":
            raise InvalidTransition("result requires running run")
        await EvidenceRepository(s).insert_batch(ctx, ref, batch.evidence)
        graph = GraphRepository(s)
        await graph.insert_batch(ctx, ref, batch.nodes, [])
        existing_links = set(
            (
                await s.execute(
                    select(evidence_derivations.c.child_id, evidence_derivations.c.parent_id).where(
                        *scope_filter(evidence_derivations, ref)
                    )
                )
            )
            .tuples()
            .all()
        )
        for d in batch.derivations:
            if (d.child_id, d.parent_id) not in existing_links:
                await s.execute(
                    insert(evidence_derivations).values(
                        org_id=ref.org_id,
                        snapshot_id=ref.snapshot_id,
                        run_id=ref.run_id,
                        **d.model_dump(),
                    )
                )
        await graph.insert_batch(ctx, ref, [], batch.edges)
        # A result hash represents the entire published set, including staged inserts.
        for table, key, expected in [
            (cpg_nodes, "local_id", {n.local_id for n in batch.nodes}),
            (evidence_records, "id", {e.id for e in batch.evidence}),
            (cpg_edges, "id", {e.id for e in batch.edges}),
        ]:
            actual = set(
                (await s.scalars(select(table.c[key]).where(*scope_filter(table, ref)))).all()
            )
            if actual != expected:
                raise DuplicateConflict("batch omits previously staged records")
        if existing_links - {(d.child_id, d.parent_id) for d in batch.derivations}:
            raise DuplicateConflict("batch omits previously staged derivations")
        row = (
            (
                await s.execute(
                    update(analysis_runs)
                    .where(*run_filter(ref))
                    .values(
                        status=batch.disposition,
                        write_version=old.write_version + 1,
                        result_hash=result_hash,
                        coverage=batch.coverage.model_dump(),
                        limitations=batch.limitations,
                        finished_at=datetime.now(UTC),
                    )
                    .returning(analysis_runs)
                )
            )
            .mappings()
            .one()
        )
        result = RunRecord(**{k: row[k] for k in RunRecord.model_fields})
        return result

    async def read_published_graph(
        self,
        ctx: TenantContext,
        ref: RunRef,
        limit: int = 200,
        after: str | UUID | None = None,
    ) -> GraphPage:
        """Edge pages include endpoints; trailing node pages cover disconnected nodes.

        Consumers merge records by scoped identity; endpoint nodes may repeat.
        """
        if not 1 <= limit <= 200:
            raise ValueError("limit must be 1..200")
        phase, cursor = "edge", None
        if after is not None:
            value = "edge:" + str(after) if isinstance(after, UUID) else after
            kind, separator, raw = value.partition(":")
            if not separator or kind not in {"edge", "node"}:
                raise ValueError("invalid graph cursor")
            phase, cursor = kind, UUID(raw) if raw else None
        async with transaction(self.factory) as s:
            await AnalysisRepository(s).get(ctx, ref, published=True)
            if phase == "edge":
                q = select(cpg_edges).where(*scope_filter(cpg_edges, ref))
                if cursor is not None:
                    q = q.where(cpg_edges.c.id > cursor)
                rows = list(
                    (await s.execute(q.order_by(cpg_edges.c.id).limit(limit + 1))).mappings()
                )
                edges = [edge_dto(r) for r in rows[:limit]]
                if edges:
                    ids = {e.source_local_id for e in edges} | {e.target_local_id for e in edges}
                    nq = select(cpg_nodes).where(
                        *scope_filter(cpg_nodes, ref), cpg_nodes.c.local_id.in_(ids)
                    )
                    nodes = [
                        node_dto(r)
                        for r in (await s.execute(nq.order_by(cpg_nodes.c.local_id))).mappings()
                    ]
                    extra_nodes = await s.scalar(
                        select(func.count())
                        .select_from(cpg_nodes)
                        .where(*scope_filter(cpg_nodes, ref), cpg_nodes.c.local_id.not_in(ids))
                    )
                    next_cursor = (
                        "edge:" + str(edges[-1].id)
                        if len(rows) > limit
                        else "node:"
                        if extra_nodes
                        else None
                    )
                    return GraphPage(
                        scope=ref,
                        edges=edges,
                        nodes=nodes,
                        truncated=next_cursor is not None,
                        next_cursor=next_cursor,
                    )
                cursor = None
            nq = select(cpg_nodes).where(*scope_filter(cpg_nodes, ref))
            if cursor is not None:
                nq = nq.where(cpg_nodes.c.local_id > cursor)
            rows = list(
                (await s.execute(nq.order_by(cpg_nodes.c.local_id).limit(limit + 1))).mappings()
            )
            nodes = [node_dto(r) for r in rows[:limit]]
            next_cursor = "node:" + str(nodes[-1].local_id) if len(rows) > limit else None
            return GraphPage(
                scope=ref,
                edges=[],
                nodes=nodes,
                truncated=next_cursor is not None,
                next_cursor=next_cursor,
            )
