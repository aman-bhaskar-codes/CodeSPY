"""Evidence-backed repository memory service for persistent findings and decisions."""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import ColumnElement, Select, func, null, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.memory import (
    EvidencePointerV1,
    MemoryCreateRequestV1,
    MemoryEntryV1,
    MemoryHistoryV1,
    MemoryKind,
    MemoryManagementPageV1,
    MemoryQueryRequestV1,
    MemoryQueryResponseV1,
    MemoryReviewEventV1,
    MemoryReviewRequestV1,
    MemoryState,
)
from app.contracts.search import SearchScopeV1
from app.contracts.source_view import PrivacyPolicyV1
from app.db.time import database_now
from app.domain.canonical_json import semantic_digest
from app.domain.errors import DuplicateConflict, InvalidTransition, PermissionDenied, ScopeViolation
from app.domain.scope import OrgRole
from app.models.chunk_tables import source_chunks
from app.models.memory_tables import (
    memory_commands,
    memory_review_events,
    memory_versions,
    repo_memories,
)
from app.models.phase2_tables import PHASE2_TABLES
from app.models.scan_tables import finding_occurrences
from app.models.tables import artifacts, repositories, source_files, source_snapshots
from app.ports.memory import MemoryEvidencePort
from app.security.authorization import require_repository
from app.services.privacy_transform import transform_model_safe
from app.services.retrieval_selection import (
    resolve_lexical_set,
    selected_chunks,
    validate_safe_content,
)


class MemoryService:
    """Manages creation and scoped retrieval of evidence-backed repository memories."""

    def __init__(
        self, session: AsyncSession, *, evidence: MemoryEvidencePort | None = None
    ) -> None:
        self.session = session
        self.evidence = evidence

    async def record_memory(self, request: MemoryCreateRequestV1) -> MemoryEntryV1:
        """Propose bounded repository knowledge; proposals are never trusted automatically."""
        org, repo, snap, actor = (
            request.org_id,
            request.repository_id,
            request.snapshot_id,
            request.principal_id,
        )
        if org is None or repo is None or snap is None or actor is None:
            raise ValueError("bound memory scope is required")
        await require_repository(self.session, org, actor, repo, write=True)
        digest = semantic_digest(request.model_dump(exclude={"request_key"}))
        if request.request_key:
            replay = await self._replay(org, repo, actor, request.request_key, digest)
            if replay is not None:
                if not await self.session.scalar(
                    self._snapshot_available(org, repo, replay.snapshot_id)
                ):
                    raise ScopeViolation("memory source unavailable")
                return replay
        if not await self.session.scalar(self._snapshot_available(org, repo, snap)):
            raise ScopeViolation("Memory snapshot is unavailable in repository")
        for pointer in request.evidence_pointers:
            await self._verify_pointer(org, repo, snap, actor, pointer)
        await self._verify_scope(org, repo, actor, write=True)
        await self._lock_repository(org, repo)
        digest = semantic_digest(request.model_dump(exclude={"request_key"}))
        if request.request_key:
            replay = await self._replay(org, repo, actor, request.request_key, digest)
            if replay is not None:
                return replay
        if not await self.session.scalar(self._snapshot_available(org, repo, snap)):
            raise ScopeViolation("Memory snapshot is unavailable in repository")
        now = self._aware(await database_now(self.session))
        entry = MemoryEntryV1(
            memory_id=uuid4(),
            org_id=org,
            repository_id=repo,
            snapshot_id=snap,
            memory_kind=request.memory_kind,
            topic=request.topic,
            summary=request.summary,
            evidence_pointers=request.evidence_pointers,
            created_at=now,
            updated_at=now,
            state=MemoryState.CANDIDATE,
            expires_at=now + timedelta(days=7),
            content_digest=semantic_digest(
                {
                    "topic": request.topic,
                    "summary": request.summary,
                    "kind": request.memory_kind,
                    "pointers": [p.model_dump(mode="json") for p in request.evidence_pointers],
                }
            ),
            source_correspondence="original_snapshot"
            if request.evidence_pointers
            else "human_note",
        )
        await self.session.execute(
            repo_memories.insert().values(
                org_id=org,
                id=entry.memory_id,
                repository_id=repo,
                snapshot_id=snap,
                memory_kind=entry.memory_kind,
                topic=entry.topic,
                summary=entry.summary,
                evidence_pointers=[p.model_dump(mode="json") for p in entry.evidence_pointers],
                state=entry.state,
                version=1,
                creator_id=actor,
                content_digest=entry.content_digest,
                expires_at=entry.expires_at,
                purge_after=now + timedelta(days=14),
                created_at=now,
                updated_at=now,
            )
        )
        await self._save_version(entry)
        if request.request_key:
            await self._receipt(entry, actor, request.request_key, digest)
        return entry

    async def review_memory(
        self,
        org: UUID,
        repo: UUID,
        actor: UUID,
        memory_id: UUID,
        request: MemoryReviewRequestV1,
    ) -> MemoryEntryV1:
        """Serialize review with current repository authority and immutable command receipts."""
        ctx = await require_repository(self.session, org, actor, repo, write=True)
        if ctx.role not in (OrgRole.OWNER, OrgRole.ADMIN):
            raise PermissionDenied("memory_review_forbidden")
        digest = semantic_digest({"memory_id": memory_id, "command": request.model_dump()})
        replay = await self._replay(org, repo, actor, request.request_key, digest)
        if replay is not None:
            if not await self.session.scalar(
                self._snapshot_available(org, repo, replay.snapshot_id)
            ):
                raise ScopeViolation("memory source unavailable")
            return replay
        if request.action in {"accept", "restore"}:
            before = (
                (
                    await self.session.execute(
                        select(repo_memories).where(
                            repo_memories.c.org_id == org,
                            repo_memories.c.repository_id == repo,
                            repo_memories.c.id == memory_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if before is None:
                raise ScopeViolation("memory unavailable")
            for raw in before["evidence_pointers"]:
                await self._verify_pointer(
                    org, repo, before["snapshot_id"], actor, EvidencePointerV1(**raw)
                )
        ctx = await require_repository(self.session, org, actor, repo, write=True, lock=True)
        if ctx.role not in (OrgRole.OWNER, OrgRole.ADMIN):
            raise PermissionDenied("memory_review_forbidden")
        await self._lock_repository(org, repo)
        digest = semantic_digest({"memory_id": memory_id, "command": request.model_dump()})
        replay = await self._replay(org, repo, actor, request.request_key, digest)
        if replay is not None:
            return replay
        row = (
            (
                await self.session.execute(
                    select(repo_memories)
                    .where(
                        repo_memories.c.org_id == org,
                        repo_memories.c.repository_id == repo,
                        repo_memories.c.id == memory_id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ScopeViolation("memory unavailable")
        now = self._aware(await database_now(self.session))
        state = self._effective_state(row, now)
        if row["version"] != request.expected_version or row["purged_at"] is not None:
            raise InvalidTransition("memory version unavailable")
        allowed = {
            "accept": {"candidate", "needs_review"},
            "reject": {"candidate", "needs_review"},
            "archive": {"accepted"},
            "restore": {"archived", "expired"},
            "supersede": {"accepted"},
        }
        if state not in allowed[request.action]:
            raise InvalidTransition("memory action unavailable")
        if request.action in {"accept", "restore"}:
            if not await self.session.scalar(
                self._snapshot_available(org, repo, row["snapshot_id"])
            ):
                raise ScopeViolation("memory source unavailable")
            for raw in row["evidence_pointers"]:
                await self._verify_pointer(
                    org,
                    repo,
                    row["snapshot_id"],
                    actor,
                    EvidencePointerV1(**raw),
                    current_only=True,
                )
        if request.action == "supersede":
            replacement = (
                (
                    await self.session.execute(
                        select(repo_memories)
                        .where(
                            repo_memories.c.org_id == org,
                            repo_memories.c.repository_id == repo,
                            repo_memories.c.id == request.replacement_id,
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if (
                replacement is None
                or replacement["id"] == memory_id
                or self._effective_state(replacement, now) != "accepted"
            ):
                raise InvalidTransition("replacement must be separately reviewed and accepted")
        new_state = {
            "accept": "accepted",
            "restore": "accepted",
            "reject": "rejected",
            "archive": "archived",
            "supersede": "superseded",
        }[request.action]
        accepted = new_state == "accepted"
        values: dict[str, Any] = {
            "state": new_state,
            "version": row["version"] + 1,
            "reviewer_id": actor,
            "updated_at": now,
            "purge_after": now + timedelta(days=97 if accepted else 7),
        }
        if accepted:
            values.update(accepted_at=now, expires_at=now + timedelta(days=90))
        await self.session.execute(
            repo_memories.update()
            .where(repo_memories.c.org_id == org, repo_memories.c.id == memory_id)
            .values(**values)
        )
        updated = dict(row) | values
        entry = self._entry(updated, now)
        await self._save_version(entry)
        await self.session.execute(
            memory_review_events.insert().values(
                org_id=org,
                repository_id=repo,
                id=uuid4(),
                memory_id=memory_id,
                actor_id=actor,
                version=entry.version,
                action=request.action,
                reason=request.reason,
                created_at=now,
            )
        )
        await self._receipt(entry, actor, request.request_key, digest)
        return entry

    async def list_memories(
        self,
        org: UUID,
        repo: UUID,
        actor: UUID,
        *,
        snapshot_id: UUID | None = None,
        cursor: UUID | None = None,
        limit: int = 20,
    ) -> MemoryManagementPageV1:
        """Bounded management view; reading a candidate never approves it for reuse."""
        ctx = await require_repository(self.session, org, actor, repo)
        can_review = ctx.role in (OrgRole.OWNER, OrgRole.ADMIN)
        if can_review:
            try:
                await require_repository(self.session, org, actor, repo, write=True)
            except ScopeViolation:
                can_review = False
        if not 1 <= limit <= 100:
            raise ValueError("invalid page limit")
        query = select(repo_memories).where(
            repo_memories.c.org_id == org,
            repo_memories.c.repository_id == repo,
            repo_memories.c.purged_at.is_(None),
        )
        if snapshot_id:
            query = query.where(repo_memories.c.snapshot_id == snapshot_id)
        if cursor:
            query = query.where(repo_memories.c.id > cursor)
        rows = (
            (await self.session.execute(query.order_by(repo_memories.c.id).limit(limit + 1)))
            .mappings()
            .all()
        )
        now = self._aware(await database_now(self.session))
        entries = []
        for row in rows[:limit]:
            entry = self._entry(row, now)
            if not await self.session.scalar(
                self._snapshot_available(org, repo, row["snapshot_id"])
            ):
                entry = entry.model_copy(
                    update={
                        "source_available": False,
                        "topic": "[source unavailable]",
                        "summary": "",
                        "evidence_pointers": (),
                    }
                )
            entries.append(entry)
        return MemoryManagementPageV1(
            can_review=can_review,
            items=tuple(entries),
            next_cursor=rows[limit - 1]["id"] if len(rows) > limit else None,
        )

    async def history(self, org: UUID, repo: UUID, actor: UUID, memory_id: UUID) -> MemoryHistoryV1:
        await self._verify_scope(org, repo, actor)
        row = (
            (
                await self.session.execute(
                    select(repo_memories).where(
                        repo_memories.c.org_id == org,
                        repo_memories.c.repository_id == repo,
                        repo_memories.c.id == memory_id,
                        self._snapshot_available(
                            org, repo, repo_memories.c.snapshot_id
                        ).scalar_subquery(),
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ScopeViolation("memory unavailable")
        versions = (
            (
                await self.session.execute(
                    select(memory_versions.c.payload)
                    .where(
                        memory_versions.c.org_id == org,
                        memory_versions.c.memory_id == memory_id,
                        memory_versions.c.payload.is_not(None),
                    )
                    .order_by(memory_versions.c.version.desc())
                    .limit(21)
                )
            )
            .scalars()
            .all()
        )
        events = (
            (
                await self.session.execute(
                    select(memory_review_events)
                    .where(
                        memory_review_events.c.org_id == org,
                        memory_review_events.c.memory_id == memory_id,
                    )
                    .order_by(memory_review_events.c.version.desc())
                    .limit(21)
                )
            )
            .mappings()
            .all()
        )
        return MemoryHistoryV1(
            versions=tuple(MemoryEntryV1.model_validate(v) for v in versions[:20]),
            events=tuple(
                MemoryReviewEventV1(
                    version=e["version"],
                    actor_id=e["actor_id"],
                    action=e["action"],
                    reason=e["reason"],
                    created_at=e["created_at"],
                )
                for e in events[:20]
            ),
            partial=len(versions) > 20 or len(events) > 20,
        )

    async def query_memory(self, request: MemoryQueryRequestV1) -> MemoryQueryResponseV1:
        """Select only reviewed, unexpired memories with bounded provenance validation."""
        org, repo, actor = request.org_id, request.repository_id, request.principal_id
        if org is None or repo is None or actor is None:
            raise ValueError("bound memory scope is required")
        await self._verify_scope(org, repo, actor)
        if request.snapshot_id is None and request.history_mode != "off":
            raise ValueError("memory_snapshot_unbound")
        if request.snapshot_id is not None and not await self.session.scalar(
            self._snapshot_available(org, repo, request.snapshot_id)
        ):
            raise ScopeViolation("Memory snapshot is unavailable in repository")
        if request.history_mode == "off":
            return MemoryQueryResponseV1(
                org_id=org, repository_id=repo, query=request.query, total_candidates=0, items=()
            )
        now = self._aware(await database_now(self.session))
        query = select(repo_memories).where(
            repo_memories.c.org_id == org,
            repo_memories.c.repository_id == repo,
            repo_memories.c.state == "accepted",
            repo_memories.c.expires_at > now,
            repo_memories.c.purged_at.is_(None),
            self._snapshot_available(org, repo, repo_memories.c.snapshot_id).scalar_subquery(),
        )
        if request.snapshot_id is not None and request.history_mode == "current_only":
            query = query.where(repo_memories.c.snapshot_id == request.snapshot_id)
        if request.memory_kind is not None:
            query = query.where(repo_memories.c.memory_kind == request.memory_kind)
        if request.topic is not None:
            query = query.where(repo_memories.c.topic == request.topic)
        terms = request.query.split()[:32]
        if self.session.bind is not None and self.session.bind.dialect.name == "postgresql":
            expression = func.plainto_tsquery("simple", " ".join(terms))
            query = query.where(repo_memories.c.tsv.op("@@")(expression))
            rank = func.ts_rank_cd(repo_memories.c.tsv, expression).desc()
        else:
            corpus = func.lower(repo_memories.c.topic + " " + repo_memories.c.summary)
            query = query.where(or_(*(corpus.contains(t.lower(), autoescape=True) for t in terms)))
            rank = repo_memories.c.accepted_at.desc()
        if request.snapshot_id:
            query = query.order_by((repo_memories.c.snapshot_id == request.snapshot_id).desc())
        rows = (
            (await self.session.execute(query.order_by(rank, repo_memories.c.id).limit(100)))
            .mappings()
            .all()
        )
        items: list[MemoryEntryV1] = []
        reasons: list[str] = []
        historical = 0
        for row in rows:
            is_history = (
                request.snapshot_id is not None and row["snapshot_id"] != request.snapshot_id
            )
            if len(items) >= request.limit or (is_history and historical >= 2):
                reasons.append("selection_limit")
                continue
            try:
                pointers = tuple(EvidencePointerV1(**p) for p in row["evidence_pointers"])
                for pointer in pointers:
                    await self._verify_pointer(
                        org, repo, row["snapshot_id"], actor, pointer, current_only=True
                    )
            except (ScopeViolation, ValueError):
                reasons.append("source_unavailable")
                continue
            correspondence = "original_snapshot" if pointers else "human_note"
            if is_history and pointers:
                paths = tuple(p.file_path for p in pointers)
                original = {
                    str(k): str(v)
                    for k, v in (
                        (
                            await self.session.execute(
                                select(source_files.c.path, source_files.c.sha256).where(
                                    source_files.c.org_id == org,
                                    source_files.c.snapshot_id == row["snapshot_id"],
                                    source_files.c.path.in_(paths),
                                )
                            )
                        ).all()
                    )
                }
                current = {
                    str(k): str(v)
                    for k, v in (
                        (
                            await self.session.execute(
                                select(source_files.c.path, source_files.c.sha256).where(
                                    source_files.c.org_id == org,
                                    source_files.c.snapshot_id == request.snapshot_id,
                                    source_files.c.path.in_(paths),
                                )
                            )
                        ).all()
                    )
                }
                correspondence = (
                    "matching_files"
                    if original == current and len(current) == len(set(paths))
                    else "changed"
                )
            entry = self._entry(row, now).model_copy(
                update={
                    "historical_hint": is_history,
                    "source_correspondence": correspondence,
                    "summary": transform_model_safe(
                        row["summary"].encode(), PrivacyPolicyV1()
                    ).safe_text
                    or "",
                    "topic": transform_model_safe(
                        row["topic"].encode(), PrivacyPolicyV1()
                    ).safe_text
                    or "",
                }
            )
            items.append(entry)
            historical += int(is_history)
        await self._verify_scope(org, repo, actor)
        if items:
            eligible = set(
                (
                    await self.session.execute(
                        select(repo_memories.c.id, repo_memories.c.version).where(
                            repo_memories.c.org_id == org,
                            repo_memories.c.repository_id == repo,
                            repo_memories.c.id.in_([i.memory_id for i in items]),
                            repo_memories.c.state == "accepted",
                            repo_memories.c.expires_at
                            > self._aware(await database_now(self.session)),
                            self._snapshot_available(
                                org, repo, repo_memories.c.snapshot_id
                            ).scalar_subquery(),
                        )
                    )
                ).all()
            )
            removed = [i for i in items if (i.memory_id, i.version) not in eligible]
            items = [i for i in items if (i.memory_id, i.version) in eligible]
            reasons.extend("memory_changed" for _ in removed)
        return MemoryQueryResponseV1(
            org_id=org,
            repository_id=repo,
            query=request.query,
            total_candidates=len(rows),
            scanned_count=len(rows),
            omitted_count=len(reasons),
            partial=len(rows) == 100,
            omission_reasons=tuple(sorted(set(reasons))),
            items=tuple(items),
        )

    async def purge_expired(self, limit: int = 100) -> int:
        """Bounded operator-only cleanup; never delete identity, review or command audit."""
        if not 1 <= limit <= 100:
            raise ValueError("invalid cleanup batch")
        now = self._aware(await database_now(self.session))
        rows = (
            (
                await self.session.execute(
                    select(repo_memories)
                    .where(
                        repo_memories.c.purged_at.is_(None),
                        repo_memories.c.purge_after <= now,
                    )
                    .order_by(repo_memories.c.purge_after, repo_memories.c.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            .mappings()
            .all()
        )
        for row in rows:
            await self.session.execute(
                memory_versions.update()
                .where(
                    memory_versions.c.org_id == row["org_id"],
                    memory_versions.c.memory_id == row["id"],
                    memory_versions.c.payload.is_not(None),
                )
                .values(payload=null(), purged_at=now)
            )
            await self.session.execute(
                repo_memories.update()
                .where(
                    repo_memories.c.org_id == row["org_id"],
                    repo_memories.c.id == row["id"],
                )
                .values(topic="[purged]", summary="", evidence_pointers=[], tsv=None, purged_at=now)
            )
        return len(rows)

    async def assert_current(
        self,
        org: UUID,
        repo: UUID,
        actor: UUID,
        references: tuple[tuple[UUID, int, str], ...],
        *,
        target_snapshot_id: UUID | None = None,
    ) -> None:
        """Call inside dispatch authority transaction; never hold these locks across network I/O."""
        await require_repository(self.session, org, actor, repo, write=True, lock=True)
        await self._lock_repository(org, repo)
        if target_snapshot_id is not None:
            await self._lock_retained_source(org, repo, target_snapshot_id)
        for memory_id, version, digest in sorted(references, key=lambda item: str(item[0])):
            row = (
                (
                    await self.session.execute(
                        select(repo_memories)
                        .where(
                            repo_memories.c.org_id == org,
                            repo_memories.c.repository_id == repo,
                            repo_memories.c.id == memory_id,
                        )
                        .with_for_update(read=True)
                    )
                )
                .mappings()
                .one_or_none()
            )
            now = self._aware(await database_now(self.session))
            if (
                row is None
                or self._effective_state(row, now) != "accepted"
                or row["version"] != version
                or row["content_digest"] != digest
            ):
                raise InvalidTransition("context_invalidated")
            await self._lock_retained_source(org, repo, row["snapshot_id"])
            for pointer in row["evidence_pointers"]:
                await self._verify_pointer(
                    org,
                    repo,
                    row["snapshot_id"],
                    actor,
                    EvidencePointerV1(**pointer),
                    current_only=True,
                )

    async def _lock_retained_source(self, org: UUID, repo: UUID, snapshot_id: UUID) -> None:
        from app.services.analysis_sources import lock_retained_snapshot

        await lock_retained_snapshot(self.session, org, repo, snapshot_id)

    async def _lock_repository(self, org: UUID, repo: UUID) -> None:
        await self.session.scalar(
            select(repositories.c.id)
            .where(repositories.c.org_id == org, repositories.c.id == repo)
            .with_for_update()
        )

    async def _replay(
        self, org: UUID, repo: UUID, actor: UUID, key: str, digest: str
    ) -> MemoryEntryV1 | None:
        receipt = (
            (
                await self.session.execute(
                    select(memory_commands).where(
                        memory_commands.c.org_id == org,
                        memory_commands.c.repository_id == repo,
                        memory_commands.c.request_key == key,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if receipt is None:
            return None
        if receipt["actor_id"] != actor or receipt["request_digest"] != digest:
            raise DuplicateConflict("memory request conflict")
        payload = await self.session.scalar(
            select(memory_versions.c.payload).where(
                memory_versions.c.org_id == org,
                memory_versions.c.memory_id == receipt["memory_id"],
                memory_versions.c.version == receipt["version"],
            )
        )
        if payload is None:
            raise InvalidTransition("memory payload expired")
        return MemoryEntryV1.model_validate(payload)

    async def _save_version(self, entry: MemoryEntryV1) -> None:
        await self.session.execute(
            memory_versions.insert().values(
                org_id=entry.org_id,
                repository_id=entry.repository_id,
                memory_id=entry.memory_id,
                version=entry.version,
                content_digest=entry.content_digest,
                payload=entry.model_dump(mode="json"),
                created_at=entry.updated_at,
            )
        )

    async def _receipt(self, entry: MemoryEntryV1, actor: UUID, key: str, digest: str) -> None:
        await self.session.execute(
            memory_commands.insert().values(
                org_id=entry.org_id,
                repository_id=entry.repository_id,
                request_key=key,
                actor_id=actor,
                request_digest=digest,
                memory_id=entry.memory_id,
                version=entry.version,
            )
        )

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value

    @classmethod
    def _effective_state(cls, row: Mapping[Any, Any], now: datetime) -> str:
        if row["state"] in {"candidate", "accepted", "needs_review"} and (
            row["expires_at"] is None or cls._aware(row["expires_at"]) <= now
        ):
            return "expired"
        return str(row["state"])

    @classmethod
    def _entry(cls, row: Mapping[Any, Any], now: datetime) -> MemoryEntryV1:
        return MemoryEntryV1(
            memory_id=row["id"],
            org_id=row["org_id"],
            repository_id=row["repository_id"],
            snapshot_id=row["snapshot_id"],
            memory_kind=MemoryKind(row["memory_kind"]),
            topic=row["topic"],
            summary=row["summary"],
            evidence_pointers=tuple(EvidencePointerV1(**p) for p in row["evidence_pointers"]),
            created_at=cls._aware(row["created_at"]),
            updated_at=cls._aware(row["updated_at"]),
            state=MemoryState(cls._effective_state(row, now)),
            version=row["version"],
            expires_at=cls._aware(row["expires_at"]) if row["expires_at"] else None,
            accepted_at=cls._aware(row["accepted_at"]) if row["accepted_at"] else None,
            reviewer_id=row["reviewer_id"],
            content_digest=row["content_digest"],
            source_correspondence="original_snapshot" if row["evidence_pointers"] else "human_note",
        )

    def _snapshot_available(
        self, org_id: UUID, repository_id: UUID, snapshot_id: UUID | ColumnElement[UUID]
    ) -> Select[Any]:
        objects = PHASE2_TABLES["object_records"]
        return select(
            select(source_snapshots.c.id)
            .join(
                artifacts,
                (artifacts.c.org_id == source_snapshots.c.org_id)
                & (artifacts.c.repository_id == source_snapshots.c.repository_id)
                & (artifacts.c.id == source_snapshots.c.artifact_id),
            )
            .join(
                objects,
                (objects.c.org_id == artifacts.c.org_id)
                & (objects.c.repository_id == artifacts.c.repository_id)
                & (objects.c.key == artifacts.c.storage_key)
                & (objects.c.sha256 == artifacts.c.sha256)
                & (objects.c.size_bytes == artifacts.c.size_bytes),
            )
            .where(
                source_snapshots.c.org_id == org_id,
                source_snapshots.c.repository_id == repository_id,
                source_snapshots.c.id == snapshot_id,
                source_snapshots.c.state == "sealed",
                objects.c.kind == "source",
                objects.c.state == "available",
            )
            .correlate(repo_memories)
            .exists()
        )

    async def _verify_pointer(
        self,
        org_id: UUID,
        repository_id: UUID,
        snapshot_id: UUID,
        principal_id: UUID,
        pointer: EvidencePointerV1,
        *,
        current_only: bool = False,
    ) -> None:
        if self.evidence is not None:
            if current_only:
                await self.evidence.verify_current(
                    org_id, repository_id, snapshot_id, principal_id, pointer
                )
            else:
                await self.evidence.verify(
                    org_id, repository_id, snapshot_id, principal_id, pointer
                )
        elif (pointer.chunk_id is None and not current_only) or pointer.finding_id is not None:
            raise ScopeViolation("memory evidence reader unavailable")
        file_id = await self.session.scalar(
            select(source_files.c.id).where(
                source_files.c.org_id == org_id,
                source_files.c.snapshot_id == snapshot_id,
                source_files.c.path == pointer.file_path,
            )
        )
        if file_id is None or pointer.end_line < pointer.start_line:
            raise ScopeViolation("Memory evidence file or range is invalid")
        if pointer.chunk_id is None and pointer.sha256 is not None:
            digest = await self.session.scalar(
                select(source_files.c.sha256).where(
                    source_files.c.org_id == org_id,
                    source_files.c.snapshot_id == snapshot_id,
                    source_files.c.id == file_id,
                )
            )
            if digest != pointer.sha256:
                raise ScopeViolation("Memory evidence source digest differs")
        if pointer.chunk_id is not None:
            if (
                pointer.chunk_set_id is None
                or pointer.materialization_hash is None
                or pointer.safe_content_sha256 is None
            ):
                raise ScopeViolation("Memory evidence requires qualified chunk provenance")
            scope = SearchScopeV1(
                org_id=org_id,
                repository_id=repository_id,
                snapshot_id=snapshot_id,
                principal_id=principal_id,
                chunk_set_id=pointer.chunk_set_id,
            )
            resolved = await resolve_lexical_set(
                self.session, scope, chunk_set_id=pointer.chunk_set_id
            )
            if resolved is None or pointer.materialization_hash != resolved.materialization_hash:
                raise ScopeViolation("Memory evidence materialization is unavailable")
            if pointer.config_hash is not None and pointer.config_hash != resolved.config_hash:
                raise ScopeViolation("Memory evidence configuration differs")
            result = await self.session.execute(
                select(source_chunks).where(
                    selected_chunks(self.session, scope, resolved),
                    source_chunks.c.org_id == org_id,
                    source_chunks.c.repository_id == repository_id,
                    source_chunks.c.snapshot_id == snapshot_id,
                    source_chunks.c.file_id == file_id,
                    source_chunks.c.id == pointer.chunk_id,
                    source_chunks.c.file_path == pointer.file_path,
                    source_chunks.c.start_line <= pointer.start_line,
                    source_chunks.c.end_line >= pointer.end_line,
                )
            )
            chunk = result.mappings().one_or_none()
            if chunk is None:
                raise ScopeViolation("Memory evidence chunk is outside scoped file range")
            validate_safe_content(dict(chunk))
            if chunk["safe_content_sha256"] != pointer.safe_content_sha256:
                raise ScopeViolation("Memory evidence safe-content digest differs")
            if pointer.sha256 is not None and pointer.sha256 != chunk["content_sha256"]:
                raise ScopeViolation("Memory evidence source digest differs")
        if pointer.finding_id is not None:
            finding = await self.session.scalar(
                select(finding_occurrences.c.id)
                .where(
                    finding_occurrences.c.org_id == org_id,
                    finding_occurrences.c.snapshot_id == snapshot_id,
                    finding_occurrences.c.file_id == file_id,
                    finding_occurrences.c.id == pointer.finding_id,
                )
                .limit(1)
            )
            if finding is None:
                raise ScopeViolation("Memory evidence finding is outside scoped file")

        if pointer.chunk_id is not None:
            # Recheck historical authority after all awaited evidence reads.
            current = await resolve_lexical_set(
                self.session, scope, chunk_set_id=pointer.chunk_set_id
            )
            if current is None or current.materialization_hash != pointer.materialization_hash:
                raise ScopeViolation("Memory evidence materialization became unavailable")

    async def _verify_scope(
        self, org_id: UUID, repository_id: UUID, principal_id: UUID, *, write: bool = False
    ) -> None:
        """Reuse current repository authority for HTTP, worker and MCP callers."""
        await require_repository(
            self.session, org_id, principal_id, repository_id, write=write, lock=write
        )
