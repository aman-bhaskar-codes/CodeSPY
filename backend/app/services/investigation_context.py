"""Freeze reviewed context once per logical call and recheck authority at dispatch."""

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.context_manifest import ContextManifestV1, MemoryContextSettingsV1
from app.contracts.memory import MemoryEntryV1, MemoryQueryRequestV1
from app.contracts.model import ModelRequestV1, ModelRole
from app.contracts.model_configuration import ModelProfileV1
from app.contracts.search import SearchScopeV1
from app.domain.canonical_json import semantic_digest
from app.domain.errors import DuplicateConflict, InvalidTransition, ScopeViolation
from app.models.investigation_tables import investigations
from app.models.memory_tables import context_manifests, memory_versions
from app.ports.model_transport import ProviderPreparationPort
from app.security.authorization import require_repository, require_snapshot
from app.services.context_composition import ComposedContext, ContextComposer
from app.services.memory_service import MemoryService

logger = logging.getLogger(__name__)


class InvestigationContextService:
    def __init__(
        self, session: AsyncSession, memories: MemoryService, settings: MemoryContextSettingsV1
    ):
        self.session, self.memories, self.settings = session, memories, settings

    async def prepare(
        self,
        org: UUID,
        repo: UUID,
        actor: UUID,
        request: ModelRequestV1,
        profile: ModelProfileV1,
        codec: ProviderPreparationPort,
        policy_digest: str,
    ) -> tuple[ComposedContext, ContextManifestV1]:
        ctx = await require_repository(self.session, org, actor, repo, write=True)
        row = (
            (
                await self.session.execute(
                    select(investigations).where(
                        investigations.c.org_id == org,
                        investigations.c.repository_id == repo,
                        investigations.c.id == request.investigation_id,
                        investigations.c.principal_id == actor,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None or row["snapshot_id"] is None:
            raise ScopeViolation("memory_snapshot_unbound")
        scope = SearchScopeV1(
            org_id=org, repository_id=repo, principal_id=actor, snapshot_id=row["snapshot_id"]
        )
        await require_snapshot(self.session, ctx, repo, scope.snapshot_id, write=True, lock=True)
        stored = await self.session.scalar(
            select(context_manifests.c.manifest).where(
                context_manifests.c.org_id == org,
                context_manifests.c.investigation_id == request.investigation_id,
                context_manifests.c.logical_call_id == request.call_id,
            )
        )
        memories: tuple[MemoryEntryV1, ...]
        if stored is not None:
            prior = ContextManifestV1.model_validate(stored)
            if prior.scope != scope or prior.policy_digest != policy_digest:
                raise DuplicateConflict("context identity differs")
            await self.assert_current(prior)
            entries = []
            for ref in prior.memory_references:
                payload = await self.session.scalar(
                    select(memory_versions.c.payload).where(
                        memory_versions.c.org_id == org,
                        memory_versions.c.memory_id == ref.memory_id,
                        memory_versions.c.version == ref.version,
                    )
                )
                if payload is None:
                    raise InvalidTransition("context_invalidated")
                entries.append(
                    MemoryEntryV1.model_validate(payload).model_copy(
                        update={
                            "historical_hint": ref.historical_hint,
                            "source_correspondence": ref.source_correspondence,
                        }
                    )
                )
            memories = tuple(entries)
        else:
            query = next(
                (
                    m.content[:256]
                    for m in reversed(request.messages)
                    if m.role == ModelRole.USER and m.content.strip()
                ),
                "repository",
            )
            selected = await self.memories.query_memory(
                MemoryQueryRequestV1(
                    org_id=org,
                    repository_id=repo,
                    snapshot_id=scope.snapshot_id,
                    principal_id=actor,
                    query=query,
                    limit=5,
                    history_mode=self.settings.history_mode,
                )
            )
            memories = selected.items if self.settings.mode == "reviewed" else ()
        composed = ContextComposer().compose(
            request,
            profile,
            codec,
            memories=memories,
            limits=self.settings.limits,
            scope=scope,
            frozen_omissions=prior.omissions if stored is not None else (),
        )
        manifest = ContextManifestV1(
            scope=scope,
            investigation_id=request.investigation_id,
            logical_call_id=request.call_id,
            profile_digest=semantic_digest(profile.model_dump(mode="json")),
            policy_digest=policy_digest,
            selection_digest=composed.selection_digest,
            prepared_request_digest=composed.prepared.wire_digest,
            required_history_digest=composed.required_history_digest,
            memory_references=composed.memory_references,
            omissions=composed.omissions,
            limits=self.settings.limits,
            budget=composed.budget,
        )
        if stored is not None:
            if manifest != prior:
                raise DuplicateConflict("context replay differs")
        else:
            await self.session.execute(
                context_manifests.insert().values(
                    org_id=org,
                    repository_id=repo,
                    snapshot_id=scope.snapshot_id,
                    principal_id=actor,
                    investigation_id=request.investigation_id,
                    logical_call_id=request.call_id,
                    selection_digest=manifest.selection_digest,
                    manifest=manifest.model_dump(mode="json"),
                )
            )
        logger.info(
            "investigation_context_prepared",
            extra={
                "memory_count": len(manifest.memory_references),
                "wire_bytes": manifest.budget.wire_bytes,
                "estimated_input_tokens": manifest.budget.input_tokens,
                "omission_count": len(manifest.omissions),
                "selection_digest": manifest.selection_digest,
            },
        )
        return composed, manifest

    async def assert_current(self, manifest: ContextManifestV1) -> None:
        scope = manifest.scope
        await self.memories.assert_current(
            scope.org_id,
            scope.repository_id,
            scope.principal_id,
            tuple((r.memory_id, r.version, r.content_digest) for r in manifest.memory_references),
            target_snapshot_id=scope.snapshot_id,
        )
