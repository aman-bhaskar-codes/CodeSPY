"""An in-process workspace for tests and for sessions that need no database record."""

from dataclasses import dataclass, field

from app.contracts.workspace import FileChangeV1, SandboxHandleV1
from app.ports.sandbox import SandboxPort
from app.services.workspace.apply import apply_idempotently
from app.services.workspace.changes import Applied, post_image, touched_paths


@dataclass
class LocalWorkspace:
    sandbox: SandboxPort
    handle: SandboxHandleV1
    revision: int = 0
    _records: dict[str, tuple[int, dict[str, str | None], tuple[str, ...]]] = field(
        default_factory=dict
    )

    async def apply(
        self,
        changes: tuple[FileChangeV1, ...],
        *,
        request_digest: str | None = None,
        author: str = "agent",
    ) -> Applied:
        view, replayed = await apply_idempotently(self.sandbox, self.handle, changes)
        if not replayed:
            self.revision += 1
            if request_digest is not None:
                self._records[request_digest] = (
                    self.revision,
                    post_image(changes),
                    touched_paths(changes),
                )
        return Applied(self.revision, view.tree_hash, touched_paths(changes), replayed)

    async def find_applied(self, request_digest: str) -> Applied | None:
        record = self._records.get(request_digest)
        if record is None:
            return None
        revision, image, paths = record
        current = await self.sandbox.file_hashes(self.handle)
        if any(current.get(path) != want for path, want in image.items()):
            return None  # later edits or an undo changed the result: the request is new again
        view = await self.sandbox.status(self.handle)
        return Applied(revision, view.tree_hash, paths, True)
