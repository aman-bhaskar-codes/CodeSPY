"""Apply a change set to a sandbox once, however many times it is asked."""

from collections.abc import Sequence

from app.contracts.workspace import FileChangeV1, SandboxHandleV1, WorkspaceRevisionV1
from app.ports.sandbox import SandboxPort
from app.services.workspace.changes import already_applied


async def apply_idempotently(
    sandbox: SandboxPort, handle: SandboxHandleV1, changes: Sequence[FileChangeV1]
) -> tuple[WorkspaceRevisionV1, bool]:
    """Write the changes unless the workspace already has their result; say which happened.

    A stale base (an expected hash the file no longer has) raises ``WorkspaceConflict`` from the
    sandbox, so a change set that was applied and then overtaken by later edits is a conflict, not a
    silent second application.
    """
    if already_applied(await sandbox.file_hashes(handle), changes):
        return await sandbox.status(handle), True
    return await sandbox.write_files(handle, tuple(changes)), False
