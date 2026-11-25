"""The durable record of a session's workspace: one per session, an append-only revision log.

The sandbox holds the files; this service holds what happened to them. Every change set goes to the
sandbox first and is recorded second, in one transaction with the ``workspace.revision`` event, so
the stream never announces a revision the database lacks. If the process dies between the two, the
next attempt to apply the same changes finds the sandbox already holding their result and records
it (``recovered``) instead of writing again.
"""

import asyncio
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.workspace import FileChangeV1, SandboxHandleV1, SandboxSpecV1, SnapshotRefV1
from app.db.transaction import transaction
from app.domain.errors import ScopeViolation
from app.domain.scope import TenantContext
from app.models.agent_tables import agent_sessions
from app.models.workspace_tables import workspace_revisions, workspaces
from app.ports.sandbox import SandboxPort
from app.services.agent.event_log import AgentEventLog
from app.services.workspace.apply import apply_idempotently
from app.services.workspace.changes import (
    Applied,
    already_applied,
    change_digest,
    changes_json,
    touched_paths,
)

MAX_EVENT_PATHS = 200


@dataclass
class OpenWorkspace:
    """A session's workspace with its sandbox attached."""

    ctx: TenantContext
    id: UUID
    session_id: UUID
    handle: SandboxHandleV1
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: File hashes as of the last revision, to tell a command's changes from the edit tools'.
    known: dict[str, str] | None = None


class WorkspaceService:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        sandbox: SandboxPort,
        *,
        image: str,
        log: AgentEventLog | None = None,
    ) -> None:
        self.factory, self.sandbox, self.image, self.log = factory, sandbox, image, log

    async def open(
        self, ctx: TenantContext, session_id: UUID, repository_id: UUID, snapshot_id: UUID
    ) -> OpenWorkspace:
        """Create the session's workspace once, attach its sandbox and load the base snapshot."""
        async with transaction(self.factory) as s:
            owner = await s.scalar(
                select(agent_sessions.c.principal_id).where(
                    agent_sessions.c.org_id == ctx.org_id, agent_sessions.c.id == session_id
                )
            )
            if owner != ctx.principal_id:
                raise ScopeViolation("agent session unavailable")
            row = (
                await s.execute(
                    select(workspaces.c.id).where(
                        workspaces.c.org_id == ctx.org_id, workspaces.c.session_id == session_id
                    )
                )
            ).first()
            workspace_id = row.id if row else uuid4()
            if row is None:
                await s.execute(
                    insert(workspaces).values(
                        org_id=ctx.org_id,
                        id=workspace_id,
                        session_id=session_id,
                        repository_id=repository_id,
                        base_snapshot_id=snapshot_id,
                    )
                )
        handle = await self.sandbox.acquire(
            SandboxSpecV1(org_id=ctx.org_id, session_id=session_id, image=self.image)
        )
        await self.sandbox.materialize(
            handle,
            SnapshotRefV1(org_id=ctx.org_id, repository_id=repository_id, snapshot_id=snapshot_id),
        )
        async with transaction(self.factory) as s:
            await s.execute(
                update(workspaces)
                .where(workspaces.c.org_id == ctx.org_id, workspaces.c.id == workspace_id)
                .values(sandbox_ref=str(handle.id))
            )
        opened = OpenWorkspace(ctx, workspace_id, session_id, handle)
        opened.known = dict(await self.sandbox.file_hashes(handle))
        return opened

    def bind(self, workspace: OpenWorkspace) -> "BoundWorkspace":
        return BoundWorkspace(self, workspace)

    async def head(self, workspace: OpenWorkspace) -> int:
        async with transaction(self.factory) as s:
            return int(
                await s.scalar(
                    select(workspaces.c.head_revision).where(
                        workspaces.c.org_id == workspace.ctx.org_id,
                        workspaces.c.id == workspace.id,
                    )
                )
                or 0
            )

    async def apply(
        self,
        workspace: OpenWorkspace,
        changes: Sequence[FileChangeV1],
        *,
        request_digest: str | None = None,
        author: str = "agent",
    ) -> Applied:
        """Write the changes once and record them once, however many times this is called."""
        async with workspace.lock:
            digest = change_digest(changes)
            before = await self.sandbox.file_hashes(workspace.handle)
            if already_applied(before, changes):
                found = await self._latest(workspace, "change_id", digest)
                if found is not None:
                    view = await self.sandbox.status(workspace.handle)
                    return Applied(found["revision"], view.tree_hash, touched_paths(changes), True)
                if not self._has_effect(changes):
                    return Applied(
                        await self.head(workspace),
                        (await self.sandbox.status(workspace.handle)).tree_hash,
                        touched_paths(changes),
                        True,
                    )
                recovered = True  # applied by an earlier attempt that died before recording it
                view = await self.sandbox.status(workspace.handle)
            else:
                recovered = False
                view, _ = await apply_idempotently(self.sandbox, workspace.handle, changes)
            revision = await self._record(
                workspace,
                changes,
                before,
                view.tree_hash,
                view.revision,
                digest,
                request_digest,
                author,
                recovered,
            )
            workspace.known = dict(await self.sandbox.file_hashes(workspace.handle))
            return Applied(revision, view.tree_hash, touched_paths(changes), False, recovered)

    async def find_applied(self, workspace: OpenWorkspace, request_digest: str) -> Applied | None:
        """The earlier application of this request, if the workspace still holds its result."""
        found = await self._latest(workspace, "request_digest", request_digest)
        if found is None:
            return None
        current = await self.sandbox.file_hashes(workspace.handle)
        for entry in found["changes"]:
            if entry["op"] == "move":
                moved = current.get(entry["move_to"]) == entry["before_hash"]
                if entry["path"] in current or not moved:
                    return None
            elif current.get(entry["path"]) != entry["after_hash"]:
                return None
        view = await self.sandbox.status(workspace.handle)
        paths = tuple(
            dict.fromkeys(p for e in found["changes"] for p in (e["path"], e.get("move_to")) if p)
        )
        return Applied(found["revision"], view.tree_hash, paths, True)

    async def _latest(
        self, workspace: OpenWorkspace, column: str, value: str
    ) -> dict[str, Any] | None:
        async with transaction(self.factory) as s:
            row = (
                await s.execute(
                    select(workspace_revisions.c.revision, workspace_revisions.c.changes_json)
                    .where(
                        workspace_revisions.c.org_id == workspace.ctx.org_id,
                        workspace_revisions.c.workspace_id == workspace.id,
                        workspace_revisions.c[column] == value,
                    )
                    .order_by(workspace_revisions.c.revision.desc())
                    .limit(1)
                )
            ).first()
        return None if row is None else {"revision": int(row.revision), "changes": row.changes_json}

    @staticmethod
    def _has_effect(changes: Sequence[FileChangeV1]) -> bool:
        """False for a change set that leaves every file as it was (nothing worth recording)."""
        from app.ports.sandbox import content_hash

        return any(
            c.op != "write" or c.content is None or content_hash(c.content) != c.expected_hash
            for c in changes
        )

    async def _record(
        self,
        workspace: OpenWorkspace,
        changes: Sequence[FileChangeV1],
        before: dict[str, str] | Any,
        tree_hash: str,
        sandbox_revision: int,
        change_id: str,
        request_digest: str | None,
        author: str,
        recovered: bool,
    ) -> int:
        touched = {c.path for c in changes}
        known_before = {p: h for p, h in dict(before).items() if p in touched} | {
            c.path: c.expected_hash for c in changes if c.expected_hash
        }  # the caller's expectation wins: after a recovery the sandbox shows the new content
        return await self._record_entries(
            workspace,
            changes_json(changes, known_before),
            touched_paths(changes),
            tree_hash,
            sandbox_revision,
            change_id,
            request_digest,
            author,
            recovered,
        )

    async def sync(self, workspace: OpenWorkspace, *, author: str = "agent") -> Applied | None:
        """Record what changed in the workspace outside the edit tools, as one revision.

        A command can write any number of files. They are found by comparing file hashes with
        those of the last revision, so the record and the event say which paths changed.
        """
        async with workspace.lock:
            now = dict(await self.sandbox.file_hashes(workspace.handle))
            before = workspace.known
            if before is None:
                workspace.known = now
                return None
            if now == before:
                return None
            entries: list[dict[str, Any]] = []
            for path in sorted(now.keys() | before.keys()):
                if now.get(path) == before.get(path):
                    continue
                entries.append(
                    {
                        "op": "delete" if path not in now else "write",
                        "path": path,
                        "before_hash": before.get(path),
                        "after_hash": now.get(path),
                    }
                )
            view = await self.sandbox.status(workspace.handle)
            change_id = hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()
            paths = tuple(e["path"] for e in entries)
            revision = await self._record_entries(
                workspace,
                entries,
                paths,
                view.tree_hash,
                view.revision,
                change_id,
                None,
                author,
                False,
            )
            workspace.known = now
            return Applied(revision, view.tree_hash, paths, False)

    async def _record_entries(
        self,
        workspace: OpenWorkspace,
        entries: list[dict[str, Any]],
        paths: Sequence[str],
        tree_hash: str,
        sandbox_revision: int,
        change_id: str,
        request_digest: str | None,
        author: str,
        recovered: bool,
    ) -> int:
        ctx = workspace.ctx
        async with transaction(self.factory) as s:
            head = await s.scalar(
                select(workspaces.c.head_revision)
                .where(workspaces.c.org_id == ctx.org_id, workspaces.c.id == workspace.id)
                .with_for_update()
            )
            if head is None:
                raise ScopeViolation("workspace unavailable")
            revision = int(head) + 1
            await s.execute(
                update(workspaces)
                .where(workspaces.c.org_id == ctx.org_id, workspaces.c.id == workspace.id)
                .values(head_revision=revision)
            )
            await s.execute(
                insert(workspace_revisions).values(
                    org_id=ctx.org_id,
                    workspace_id=workspace.id,
                    revision=revision,
                    author=author,
                    change_id=change_id,
                    request_digest=request_digest,
                    changes_json=entries,
                    tree_hash=tree_hash,
                    sandbox_revision=sandbox_revision,
                    recovered=recovered,
                )
            )
            if self.log is not None:
                await self.log.append(
                    s,
                    ctx.org_id,
                    workspace.session_id,
                    "workspace.revision",
                    {
                        "revision": revision,
                        "changed_paths": list(paths)[:MAX_EVENT_PATHS],
                        "author": "user" if author == "user" else "agent",
                    },
                    event_key=f"workspace.revision.{revision}",
                )
            return revision


class BoundWorkspace:
    """A ``WorkspaceEditPort`` for one open workspace, which is what the edit tools receive."""

    def __init__(self, service: WorkspaceService, workspace: OpenWorkspace) -> None:
        self._service, self._workspace = service, workspace

    async def apply(
        self,
        changes: tuple[FileChangeV1, ...],
        *,
        request_digest: str | None = None,
        author: str = "agent",
    ) -> Applied:
        return await self._service.apply(
            self._workspace, changes, request_digest=request_digest, author=author
        )

    async def find_applied(self, request_digest: str) -> Applied | None:
        return await self._service.find_applied(self._workspace, request_digest)

    async def sync(self, *, author: str = "agent") -> Applied | None:
        return await self._service.sync(self._workspace, author=author)
