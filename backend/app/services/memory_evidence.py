"""Admitted source and scanner authority adapter; never executes repository content."""

import hashlib
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.memory import EvidencePointerV1
from app.domain.errors import ScopeViolation
from app.domain.scope import RunRef
from app.models.scan_tables import finding_occurrences
from app.models.tables import source_files
from app.ports.scan_execution import ScanCapabilities
from app.ports.source_reader import AdmittedSourceReadV1, SourceReaderPort
from app.security.authorization import require_repository
from app.services.analysis_sources import locate_source
from app.services.scan_reads import ScanReadService


class SourceMemoryEvidence:
    def __init__(
        self,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        reader: SourceReaderPort | None,
        capabilities: ScanCapabilities | None = None,
    ):
        self.session, self.factory, self.reader = session, factory, reader
        self.capabilities = capabilities

    async def verify(
        self, org: UUID, repo: UUID, snapshot: UUID, actor: UUID, pointer: EvidencePointerV1
    ) -> None:
        await require_repository(self.session, org, actor, repo)
        if pointer.chunk_id is None:
            if self.reader is None:
                raise ScopeViolation("memory evidence reader unavailable")
            located = await locate_source(self.session, org, repo, snapshot)
            file = next(
                (f for f in located.files if f.path == pointer.file_path and f.eligible), None
            )
            if file is None or file.size_bytes > 2_097_152:
                raise ScopeViolation("memory evidence source unavailable")
            content = await self.reader.read(
                AdmittedSourceReadV1(
                    org_id=org,
                    repository_id=repo,
                    snapshot_id=snapshot,
                    file_id=file.file_id,
                    archive=located.archive,
                    path=file.path,
                    sha256=file.sha256,
                    size_bytes=file.size_bytes,
                    strip_root=located.policy.github_strip_root,
                )
            )
            if (
                len(content) != file.size_bytes
                or hashlib.sha256(content).hexdigest() != file.sha256
                or (pointer.sha256 is not None and pointer.sha256 != file.sha256)
            ):
                raise ScopeViolation("memory evidence digest differs")
            try:
                lines = content.decode("utf-8").splitlines()
            except UnicodeDecodeError as exc:
                raise ScopeViolation("memory evidence is not text") from exc
            if pointer.end_line > len(lines):
                raise ScopeViolation("memory evidence range unavailable")
            current = await locate_source(self.session, org, repo, snapshot)
            if located.archive != current.archive or file not in current.files:
                raise ScopeViolation("memory evidence source unavailable")
        await self.verify_current(org, repo, snapshot, actor, pointer)

    async def verify_current(
        self, org: UUID, repo: UUID, snapshot: UUID, actor: UUID, pointer: EvidencePointerV1
    ) -> None:
        ctx = await require_repository(self.session, org, actor, repo)
        if pointer.finding_id is not None:
            row = (
                await self.session.execute(
                    select(finding_occurrences.c.run_id)
                    .join(
                        source_files,
                        (source_files.c.org_id == finding_occurrences.c.org_id)
                        & (source_files.c.snapshot_id == finding_occurrences.c.snapshot_id)
                        & (source_files.c.id == finding_occurrences.c.file_id),
                    )
                    .where(
                        finding_occurrences.c.org_id == org,
                        finding_occurrences.c.snapshot_id == snapshot,
                        finding_occurrences.c.id == pointer.finding_id,
                        source_files.c.path == pointer.file_path,
                    )
                )
            ).one_or_none()
            if row is None:
                raise ScopeViolation("memory finding unavailable")
            result = await ScanReadService(self.factory, self.capabilities).read_in_session(
                self.session, ctx, RunRef(org_id=org, snapshot_id=snapshot, run_id=row.run_id)
            )
            if result is None or not any(
                d.finding_id == pointer.finding_id and d.effective_disposition == "supported-static"
                for d in result.effective_decisions
            ):
                raise ScopeViolation("memory finding support unavailable")
        await require_repository(self.session, org, actor, repo)
