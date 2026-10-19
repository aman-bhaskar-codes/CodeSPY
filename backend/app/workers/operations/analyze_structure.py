"""Orchestrate verified inputs, isolated analysis and one fenced publication."""

import asyncio
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

from app.contracts.jobs import AttemptLeaseV2
from app.contracts.structure import (
    ArchitectureReportV1,
    SourceInputV1,
    StructuralDraftV1,
    stored_config,
)
from app.db.transaction import transaction
from app.domain.errors import DuplicateConflict
from app.models.phase3_tables import analysis_specs
from app.ports.object_store import ObjectStore
from app.services.analysis_sources import locate_source, stage_source
from app.services.jobs import JobService
from app.services.structural_finalization import StructuralFinalizer
from app.services.structural_results import materialize_result
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class AnalysisRunner(Protocol):
    async def analyze(self, source: SourceInputV1, root: Path) -> StructuralDraftV1: ...


class StructuralAnalysisOperation:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        store: ObjectStore,
        finalizer: StructuralFinalizer,
        runner: AnalysisRunner,
    ) -> None:
        self.factory, self.store, self.finalizer, self.runner = factory, store, finalizer, runner
        self.jobs = JobService(factory)

    async def execute(
        self,
        lease: AttemptLeaseV2,
        *,
        enter_finalization: Callable[[], Awaitable[None]] | None = None,
    ) -> ArchitectureReportV1:
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
            if config.semantic_hash() != job["config_hash"]:
                raise DuplicateConflict("stored analysis configuration differs")
            located = await locate_source(s, lease.org_id, job["repository_id"], job["snapshot_id"])
        # Close DB locks before streaming, native analysis and result validation.
        with tempfile.TemporaryDirectory(prefix="codespy-analysis-") as directory:
            root = Path(directory)
            source = await stage_source(self.store, located, config, root)
            draft = await self.runner.analyze(source, root)
            result = await asyncio.to_thread(
                materialize_result, lease.run_ref, draft, source, root, lease.heartbeat_at
            )
        if enter_finalization is not None:
            await enter_finalization()
        return await self.finalizer.finish_analysis(lease, result)
