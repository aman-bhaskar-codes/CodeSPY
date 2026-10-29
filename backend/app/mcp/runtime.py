"""Request-scoped MCP composition without provider or worker runtimes."""

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.errors import ScopeViolation
from app.domain.scope import TenantContext
from app.mcp.catalogue import CONTRACTS
from app.mcp.server import ReadOnlyMcpServer
from app.ports.object_store import ObjectStore
from app.ports.source_reader import SourceReaderPort
from app.security.authorization import require_repository, require_snapshot
from app.services.context_assembly import ContextAssemblyService
from app.services.dense_search import DenseSearchService
from app.services.feature_trace import FeatureTraceService
from app.services.findings_query import FindingsQueryService
from app.services.hybrid_search import HybridSearchService
from app.services.index_jobs import IndexJobService
from app.services.investigation_queries import InvestigationQueryService
from app.services.lexical_search import LexicalSearchService
from app.services.memory_evidence import SourceMemoryEvidence
from app.services.memory_service import MemoryService
from app.services.source_view import SourceViewService


class ConnectedMcpServer(ReadOnlyMcpServer):
    """Open one isolated session for each call and recheck current authority."""

    def __init__(
        self,
        *,
        identity: TenantContext,
        repository_allowlist: frozenset[UUID],
        factory: async_sessionmaker[AsyncSession],
        store: ObjectStore | None = None,
        reader: SourceReaderPort | None = None,
    ) -> None:
        super().__init__(identity=identity, repository_allowlist=repository_allowlist)
        self.factory, self.store, self.reader = factory, store, reader

    async def _dispatch_tool(self, name: str, args: dict[str, Any]) -> str:
        if name not in CONTRACTS:
            raise ValueError("Unknown or unauthorized tool")
        if {"org_id", "principal_id", "role"}.intersection(args):
            raise ScopeViolation("Tool arguments cannot select an identity")
        request = CONTRACTS[name][0].model_validate(args)
        repo = UUID(str(request.model_dump()["repository_id"]))
        if repo not in (self.repository_allowlist or ()) or self._identity is None:
            raise ScopeViolation("repository unavailable")
        async with self.factory() as session, session.begin():
            if hasattr(request, "snapshot_id"):
                await require_snapshot(
                    session, self._identity, repo, UUID(str(request.model_dump()["snapshot_id"]))
                )
            else:
                await require_repository(
                    session, self._identity.org_id, self._identity.principal_id, repo
                )
            lexical = LexicalSearchService(session)
            # Explicitly omit the embedding port: paid query embeddings cannot run.
            hybrid = HybridSearchService(
                session, lexical_service=lexical, dense_service=DenseSearchService(session)
            )
            facade = ReadOnlyMcpServer(
                identity=self._identity,
                repository_allowlist=self.repository_allowlist,
                db_factory=self.factory,
                lexical_service=lexical,
                hybrid_service=hybrid,
                context_service=ContextAssemblyService(session),
                source_view_service=(
                    SourceViewService(
                        session,
                        self.store,
                        reader=self.reader,
                        persist_metadata=False,
                    )
                    if self.store
                    else None
                ),
                memory_service=MemoryService(
                    session,
                    evidence=SourceMemoryEvidence(
                        session,
                        self.factory,
                        self.reader,
                    ),
                ),
                index_service=IndexJobService(self.factory),
                investigation_query_service=InvestigationQueryService(self.factory),
                findings_service=FindingsQueryService(self.factory),
                feature_trace_service=FeatureTraceService(self.factory),
            )
            result = await facade.dispatch_tool(name, args)
            if hasattr(request, "snapshot_id"):
                await require_snapshot(
                    session, self._identity, repo, UUID(str(request.model_dump()["snapshot_id"]))
                )
            else:
                await require_repository(
                    session, self._identity.org_id, self._identity.principal_id, repo
                )
            return result


# Source isolation is opt-in and uses exactly the same container envelope as API reads.
# The caller supplies the object store and an already qualified, digest-pinned image.
