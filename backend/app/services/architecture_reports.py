# ruff: noqa: E501
"""Plan, build, store and read architecture reports over published AST facts.

`prepare` partitions a snapshot's analyzable files into shards of at most 128 files and creates one
existing AST-facts analysis per shard (within the organization's pending-job quota; call it again to
create the rest). `build` waits for nothing: it requires every shard analysis to be published, then
computes the report as a pure function of those facts and stores it immutably.
"""

import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analyzers.facts import FACT_LANGUAGES
from app.analyzers.facts.manifests import is_manifest
from app.analyzers.graph import FileEntry
from app.analyzers.graph.report import assemble_report
from app.analyzers.graph.rules import check_rules
from app.analyzers.graph.shards import ShardPlan, plan_shards
from app.contracts.architecture_report import (
    MAX_FACT_RUNS,
    ArchitectureReportRefV1,
    ArchitectureShardsV1,
    ReportTooLarge,
    RepositoryReportV1,
    ShardStatusV1,
)
from app.contracts.architecture_rules import RuleCheckV1, RuleSetV1
from app.contracts.ast_facts import FileFactsV1
from app.contracts.structure import AnalysisConfigV1
from app.db.transaction import transaction
from app.domain.errors import CapacityExceeded, DuplicateConflict, ScopeViolation
from app.domain.scope import TenantContext
from app.security.authorization import lock_scope, require_snapshot
from app.services.analysis_sources import locate_source
from app.services.structural_analysis import StructuralAnalysisService

MAX_NEW_PER_CALL = 8
TERMINAL_OK = {"succeeded", "partial"}
RETRYABLE = {"failed", "cancelled"}


class ReportInputsPending(Exception):  # noqa: N818
    """Some shard analyses are not published yet."""

    def __init__(self, shards: ArchitectureShardsV1) -> None:
        self.shards = shards
        super().__init__("report_inputs_pending")


@dataclass(frozen=True)
class SnapshotPlan:
    plan: ShardPlan
    analyzable: int
    inventory: tuple[str, ...]


def _assemble(**kwargs: Any) -> RepositoryReportV1:
    return assemble_report(**kwargs)


def shard_key(snapshot_id: UUID, plan: ShardPlan, index: int) -> str:
    """Request key of one shard; scoped to the snapshot so plans never collide across snapshots."""
    scope = hashlib.sha256(f"{snapshot_id}:{plan.digest}".encode()).hexdigest()[:32]
    return f"report:{scope}:{index}"


def _load[M: BaseModel](model: type[M], payload: Any) -> M:
    """Parse a stored JSON document through the strict contract."""
    return model.model_validate_json(payload if isinstance(payload, str) else json.dumps(payload))


async def _rows(session: AsyncSession, query: str, **params: Any) -> list[Any]:
    return list((await session.execute(text(query), params)).all())


class ArchitectureReportService:
    def __init__(
        self, factory: async_sessionmaker[AsyncSession], structural: StructuralAnalysisService
    ) -> None:
        self.factory, self.structural = factory, structural

    async def _plan(
        self, session: AsyncSession, ctx: TenantContext, repository_id: UUID, snapshot_id: UUID
    ) -> SnapshotPlan:
        located = await locate_source(session, ctx.org_id, repository_id, snapshot_id)
        eligible = [f for f in located.files if f.eligible]
        sizes = {
            f.path: f.size_bytes
            for f in eligible
            if f.language in FACT_LANGUAGES or is_manifest(f.path)
        }
        plan = plan_shards(sizes)
        if len(plan.shards) > MAX_FACT_RUNS:
            raise ReportTooLarge("repository needs more fact shards than one report can cite")
        return SnapshotPlan(
            plan=plan,
            analyzable=len(sizes),
            inventory=tuple(sorted(f.path for f in eligible)),
        )

    async def _attempts(
        self, snapshot_id: UUID, ctx: TenantContext, plan: ShardPlan, index: int
    ) -> int:
        """How many analyses were already created for one shard (the next retry's number)."""
        base = shard_key(snapshot_id, plan, index)
        async with transaction(self.factory) as session:
            rows = await _rows(
                session,
                "SELECT count(*) AS n FROM jobs WHERE org_id=:o AND snapshot_id=:s "
                "AND (request_key=:k OR request_key LIKE :kp)",
                o=ctx.org_id,
                s=snapshot_id,
                k=base,
                kp=base + ":r%",
            )
        return int(rows[0].n)

    async def _status(
        self,
        session: AsyncSession,
        ctx: TenantContext,
        repository_id: UUID,
        snapshot_id: UUID,
        plan: ShardPlan,
        skipped: int,
    ) -> ArchitectureShardsV1:
        shards = []
        for shard in plan.shards:
            found = await _rows(
                session,
                "SELECT id,state,result_run_id FROM jobs WHERE org_id=:o AND repository_id=:r "
                "AND snapshot_id=:s AND (request_key=:k OR request_key LIKE :kp) "
                "ORDER BY created_at DESC, request_key DESC LIMIT 1",
                o=ctx.org_id,
                r=repository_id,
                s=snapshot_id,
                k=shard_key(snapshot_id, plan, shard.index),
                kp=shard_key(snapshot_id, plan, shard.index) + ":r%",
            )
            job = found[0] if found else None
            shards.append(
                ShardStatusV1(
                    index=shard.index,
                    files=shard.files,
                    prefixes=len(shard.prefixes),
                    state=job.state if job else "not_started",
                    job_id=job.id if job else None,
                    run_id=job.result_run_id if job else None,
                )
            )
        return ArchitectureShardsV1(
            plan_digest=plan.digest,
            total_files=plan.total_files,
            skipped_files=skipped,
            shards=tuple(shards),
            ready=bool(shards) and all(s.state in TERMINAL_OK and s.run_id for s in shards),
        )

    async def status(
        self, ctx: TenantContext, repository_id: UUID, snapshot_id: UUID
    ) -> ArchitectureShardsV1:
        async with transaction(self.factory) as session:
            await require_snapshot(session, ctx, repository_id, snapshot_id)
            snap = await self._plan(session, ctx, repository_id, snapshot_id)
            return await self._status(
                session, ctx, repository_id, snapshot_id, snap.plan, len(snap.plan.skipped)
            )

    async def prepare(
        self,
        ctx: TenantContext,
        repository_id: UUID,
        snapshot_id: UUID,
        config_for: Callable[[tuple[str, ...]], AnalysisConfigV1],
        *,
        limit: int = MAX_NEW_PER_CALL,
    ) -> ArchitectureShardsV1:
        async with transaction(self.factory) as session:
            await require_snapshot(session, ctx, repository_id, snapshot_id, write=True)
            snap = await self._plan(session, ctx, repository_id, snapshot_id)
            current = await self._status(
                session, ctx, repository_id, snapshot_id, snap.plan, len(snap.plan.skipped)
            )
        created = 0
        for shard, state in zip(snap.plan.shards, current.shards, strict=True):
            retry = state.state in RETRYABLE
            if state.state != "not_started" and not retry:
                continue
            if created >= limit:
                break
            try:
                await self.structural.create_analysis_spec(
                    ctx,
                    repository_id,
                    snapshot_id,
                    config_for(shard.prefixes),
                    shard_key(snapshot_id, snap.plan, shard.index)
                    + (
                        f":r{await self._attempts(snapshot_id, ctx, snap.plan, shard.index)}"
                        if retry
                        else ""
                    ),
                )
            except CapacityExceeded:
                break  # the organization's pending-job quota is full; call again later
            created += 1
        async with transaction(self.factory) as session:
            return await self._status(
                session, ctx, repository_id, snapshot_id, snap.plan, len(snap.plan.skipped)
            )

    async def build(
        self, ctx: TenantContext, repository_id: UUID, snapshot_id: UUID
    ) -> ArchitectureReportRefV1:
        async with transaction(self.factory) as session:
            await lock_scope(session, ctx.org_id, ctx.principal_id, repository_id, snapshot_id)
            snap = await self._plan(session, ctx, repository_id, snapshot_id)
            status = await self._status(
                session, ctx, repository_id, snapshot_id, snap.plan, len(snap.plan.skipped)
            )
            if not status.ready:
                raise ReportInputsPending(status)
            runs = tuple(sorted({s.run_id for s in status.shards if s.run_id}))
            rows = await _rows(
                session,
                "SELECT file_id,facts_json,facts_sha256 FROM ast_file_facts "
                "WHERE org_id=:o AND snapshot_id=:s AND run_id = ANY(CAST(:runs AS uuid[]))",
                o=ctx.org_id,
                s=snapshot_id,
                runs=[str(r) for r in runs],
            )
            entries = []
            for row in rows:
                facts = _load(FileFactsV1, row.facts_json)
                entries.append(FileEntry(facts.file_id, facts.path, facts.language, facts))
            report = await asyncio.to_thread(
                _assemble,
                org_id=ctx.org_id,
                repository_id=repository_id,
                snapshot_id=snapshot_id,
                files=entries,
                inventory=snap.inventory,
                eligible_files=snap.analyzable,
                omitted_files=max(0, snap.analyzable - len(entries)),
                fact_runs=runs,
            )
            existing = await _rows(
                session,
                "SELECT id,report_digest,size_bytes FROM architecture_reports "
                "WHERE org_id=:o AND repository_id=:r AND snapshot_id=:s AND inputs_digest=:d",
                o=ctx.org_id,
                r=repository_id,
                s=snapshot_id,
                d=report.inputs_digest,
            )
            if existing:
                return self._ref(
                    existing[0].id, report, existing[0].report_digest, existing[0].size_bytes
                )
            report_id = uuid4()
            digest, size = report.digest(), report.size_bytes()
            await session.execute(
                text(
                    "INSERT INTO architecture_reports(org_id,id,repository_id,snapshot_id,principal_id,"
                    "schema_version,inputs_digest,report_digest,fact_runs,report_json,size_bytes) "
                    "VALUES(:o,:id,:r,:s,:p,1,:idig,:rdig,CAST(:runs AS jsonb),CAST(:doc AS jsonb),:size)"
                ),
                {
                    "o": ctx.org_id,
                    "id": report_id,
                    "r": repository_id,
                    "s": snapshot_id,
                    "p": ctx.principal_id,
                    "idig": report.inputs_digest,
                    "rdig": digest,
                    "runs": json.dumps([str(r) for r in report.coverage.fact_runs]),
                    "doc": json.dumps(report.model_dump(mode="json")),
                    "size": size,
                },
            )
            return self._ref(report_id, report, digest, size)

    @staticmethod
    def _ref(
        report_id: UUID, report: RepositoryReportV1, digest: str, size: int
    ) -> ArchitectureReportRefV1:
        return ArchitectureReportRefV1(
            id=report_id,
            repository_id=report.repository_id,
            snapshot_id=report.snapshot_id,
            inputs_digest=report.inputs_digest,
            report_digest=digest,
            size_bytes=size,
            complete=report.coverage.complete,
            analyzed_files=report.coverage.analyzed_files,
            eligible_files=report.coverage.eligible_files,
        )

    async def get(
        self, ctx: TenantContext, repository_id: UUID, snapshot_id: UUID, report_id: UUID
    ) -> RepositoryReportV1:
        async with transaction(self.factory) as session:
            await require_snapshot(session, ctx, repository_id, snapshot_id)
            found = await _rows(
                session,
                "SELECT report_json FROM architecture_reports WHERE org_id=:o AND id=:i "
                "AND repository_id=:r AND snapshot_id=:s",
                o=ctx.org_id,
                i=report_id,
                r=repository_id,
                s=snapshot_id,
            )
        if not found:
            raise ScopeViolation("report unavailable")
        return _load(RepositoryReportV1, found[0].report_json)

    async def check_rules(
        self,
        ctx: TenantContext,
        repository_id: UUID,
        snapshot_id: UUID,
        report_id: UUID,
        rules: RuleSetV1,
    ) -> RuleCheckV1:
        """Evaluate declared rules against a stored report; nothing is stored."""
        report = await self.get(ctx, repository_id, snapshot_id, report_id)
        return await asyncio.to_thread(
            check_rules, report.graph, rules, str(report_id), report.inputs_digest
        )

    async def latest_ref(
        self, ctx: TenantContext, repository_id: UUID, snapshot_id: UUID
    ) -> ArchitectureReportRefV1 | None:
        report_id = await self.latest(ctx, repository_id, snapshot_id)
        if report_id is None:
            return None
        report = await self.get(ctx, repository_id, snapshot_id, report_id)
        async with transaction(self.factory) as session:
            row = (
                await _rows(
                    session,
                    "SELECT report_digest,size_bytes FROM architecture_reports WHERE org_id=:o AND id=:i",
                    o=ctx.org_id,
                    i=report_id,
                )
            )[0]
        return self._ref(report_id, report, row.report_digest, row.size_bytes)

    async def latest(
        self, ctx: TenantContext, repository_id: UUID, snapshot_id: UUID
    ) -> UUID | None:
        async with transaction(self.factory) as session:
            await require_snapshot(session, ctx, repository_id, snapshot_id)
            found = await _rows(
                session,
                "SELECT id FROM architecture_reports WHERE org_id=:o AND repository_id=:r "
                "AND snapshot_id=:s ORDER BY created_at DESC,id DESC LIMIT 1",
                o=ctx.org_id,
                r=repository_id,
                s=snapshot_id,
            )
        return UUID(str(found[0].id)) if found else None


__all__ = [
    "ArchitectureReportService",
    "DuplicateConflict",
    "ReportInputsPending",
    "ReportTooLarge",
    "shard_key",
]
