"""Orchestrate verified inputs, isolated fact extraction and one fenced publication."""

import asyncio
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

from app.contracts.ast_facts import AstDraftV1, AstFileSummaryV1
from app.contracts.jobs import AttemptLeaseV2
from app.contracts.structure import SourceInputV1, stored_config
from app.db.transaction import transaction
from app.domain.errors import DuplicateConflict
from app.models.phase3_tables import analysis_specs, structural_jobs
from app.ports.object_store import ObjectStore
from app.services.analysis_sources import locate_source, stage_source
from app.services.ast_finalization import AstFactsFinalizer
from app.services.ast_results import materialize_facts
from app.services.jobs import JobService
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class FactsRunner(Protocol):
    async def analyze_facts(self, source: SourceInputV1, root: Path) -> AstDraftV1: ...


class AstFactsOperation:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        store: ObjectStore,
        finalizer: AstFactsFinalizer,
        runner: FactsRunner,
    ) -> None:
        self.factory, self.store, self.finalizer, self.runner = factory, store, finalizer, runner
        self.jobs = JobService(factory)

    async def execute(
        self,
        lease: AttemptLeaseV2,
        *,
        enter_finalization: Callable[[], Awaitable[None]] | None = None,
    ) -> list[AstFileSummaryV1]:
        if lease.run_ref is None:
            raise ValueError("analysis requires run_ref")
        async with transaction(self.factory) as s:
            job, _ = await self.jobs._locked(s, lease.org_id, lease.job_id)
            await self.jobs._current(s, lease, job)
            if job["operation"] != "analyze_structure":
                raise DuplicateConflict("job operation differs")
            spec = (
                (
                    await s.execute(
                        select(analysis_specs).where(
                            analysis_specs.c.org_id == lease.org_id,
                            analysis_specs.c.repository_id == job["repository_id"],
                            analysis_specs.c.snapshot_id == job["snapshot_id"],
                            analysis_specs.c.id == job["analysis_spec_id"],
                        )
                    )
                )
                .mappings()
                .one()
            )
            config = stored_config(spec["config_json"])
            if config.semantic_hash() != job["config_hash"] or "ast_facts" not in config.adapters:
                raise DuplicateConflict("stored analysis configuration differs")
            located = await locate_source(s, lease.org_id, job["repository_id"], job["snapshot_id"])
        with tempfile.TemporaryDirectory(prefix="codespy-ast-") as directory:
            root = Path(directory)
            source = await stage_source(self.store, located, config, root)
            draft = await self.runner.analyze_facts(source, root)
            result = await asyncio.to_thread(materialize_facts, lease.run_ref, draft, source, root)
        if enter_finalization is not None:
            await enter_finalization()
        return await self.finalizer.finish_facts(lease, result)


class ProfileDispatchOperation:
    """Route analyze_structure jobs by configuration without changing the consumer."""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        structural: Any,
        facts: AstFactsOperation,
    ) -> None:
        self.factory, self.structural, self.facts = factory, structural, facts

    async def execute(
        self,
        lease: AttemptLeaseV2,
        *,
        enter_finalization: Callable[[], Awaitable[None]] | None = None,
    ) -> Any:
        async with transaction(self.factory) as s:
            config_json = await s.scalar(
                select(analysis_specs.c.config_json)
                .join_from(
                    analysis_specs,
                    structural_jobs,
                    (structural_jobs.c.org_id == analysis_specs.c.org_id)
                    & (structural_jobs.c.analysis_spec_id == analysis_specs.c.id),
                )
                .where(
                    structural_jobs.c.org_id == lease.org_id,
                    structural_jobs.c.id == lease.job_id,
                )
            )
        if config_json is None:
            raise DuplicateConflict("analysis spec unavailable")
        target = (
            self.facts if "ast_facts" in stored_config(config_json).adapters else self.structural
        )
        return await target.execute(lease, enter_finalization=enter_finalization)
