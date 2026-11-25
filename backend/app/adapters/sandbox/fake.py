"""In-memory sandbox for tests: a real filesystem model with scripted command results.

It enforces every rule of the port (paths, hashes, atomic batches, limits, per-session isolation,
suspend and release) so the agent can be built and verified offline. Commands are never run:
a command returns what a script says, and a script may also change the workspace, to model a
build or a test run that writes files.
"""

import asyncio
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from app.adapters.sandbox.common import listing, text_slice
from app.contracts.workspace import (
    CheckpointRefV1,
    ExecChunkV1,
    ExecRequestV1,
    FileChangeV1,
    FileListingV1,
    FileSliceV1,
    SandboxCapabilitiesV1,
    SandboxHandleV1,
    SandboxSpecV1,
    SnapshotRefV1,
    WorkspaceRevisionV1,
)
from app.ports.sandbox import (
    MAX_BATCH_BYTES,
    MAX_CHANGES_PER_CALL,
    MAX_EXEC_OUTPUT_BYTES,
    MAX_FILE_BYTES,
    MAX_LIST_ENTRIES,
    MAX_WORKSPACE_FILES,
    CheckpointUnknown,
    ExecRejected,
    NotATextFile,
    PathRejected,
    SandboxGone,
    SandboxLimit,
    SandboxStateError,
    SnapshotFilesPort,
    WorkspaceConflict,
    WorkspaceFileNotFound,
    content_hash,
    normalize_workspace_path,
    tree_digest,
)


class MappingSnapshots:
    """A ``SnapshotFilesPort`` over plain dictionaries, keyed by snapshot id."""

    def __init__(self, snapshots: Mapping[UUID, Mapping[str, bytes]]) -> None:
        self._snapshots = snapshots

    async def files(self, ref: SnapshotRefV1) -> AsyncIterator[tuple[str, bytes]]:
        for path, data in self._snapshots[ref.snapshot_id].items():
            yield path, data


@dataclass
class ScriptedExec:
    """What a command does: its output, exit code, how long it takes, and what it writes."""

    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    delay_s: float = 0.0
    writes: Mapping[str, bytes] = field(default_factory=dict)


ExecScript = Callable[[ExecRequestV1, Mapping[str, bytes]], ScriptedExec]


@dataclass
class _Workspace:
    spec: SandboxSpecV1
    handle_id: UUID
    files: dict[str, bytes] = field(default_factory=dict)
    revision: int = 0
    state: str = "running"
    materialized: UUID | None = None
    checkpoints: dict[UUID, tuple[int, dict[str, bytes]]] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class FakeSandbox:
    def __init__(self, snapshots: SnapshotFilesPort, *, supports_suspend: bool = True) -> None:
        self._snapshots = snapshots
        self._supports_suspend = supports_suspend
        self._live: dict[tuple[UUID, UUID], _Workspace] = {}
        self._by_handle: dict[UUID, _Workspace] = {}
        self._scripts: list[tuple[tuple[str, ...], ScriptedExec]] = []
        self._default: ExecScript | None = None
        self.exec_calls: list[ExecRequestV1] = []
        self.cwd_calls: list[str] = []

    # --- scripting -----------------------------------------------------------------------------

    def script(self, argv_prefix: tuple[str, ...], result: ScriptedExec) -> None:
        """Commands whose argv starts with ``argv_prefix`` return ``result``. Latest script wins."""
        self._scripts.insert(0, (argv_prefix, result))

    def script_all(self, handler: ExecScript) -> None:
        self._default = handler

    # --- port ----------------------------------------------------------------------------------

    @property
    def capabilities(self) -> SandboxCapabilitiesV1:
        return SandboxCapabilitiesV1(
            supports_suspend=self._supports_suspend, isolation="fake", network_egress="none"
        )

    async def acquire(self, spec: SandboxSpecV1) -> SandboxHandleV1:
        key = (spec.org_id, spec.session_id)
        ws = self._live.get(key)
        if ws is None or ws.state == "released":
            ws = _Workspace(spec=spec, handle_id=uuid4())
            self._live[key] = ws
            self._by_handle[ws.handle_id] = ws
        return self._handle(ws)

    async def materialize(
        self, handle: SandboxHandleV1, snapshot: SnapshotRefV1
    ) -> WorkspaceRevisionV1:
        ws = self._resolve(handle)
        if snapshot.org_id != ws.spec.org_id:
            raise SandboxGone("snapshot belongs to another organisation")
        async with ws.lock:
            if ws.materialized is not None:
                if ws.materialized != snapshot.snapshot_id:
                    raise SandboxStateError("sandbox already holds another snapshot")
                return self._revision(ws, ())
            loaded: dict[str, bytes] = {}
            total = 0
            async for raw_path, data in self._snapshots.files(snapshot):
                path = normalize_workspace_path(raw_path)
                total += len(data)
                if len(loaded) >= MAX_WORKSPACE_FILES or len(data) > MAX_FILE_BYTES:
                    raise SandboxLimit("snapshot exceeds workspace limits")
                if total > ws.spec.disk_mib * 1024 * 1024:
                    raise SandboxLimit("snapshot exceeds the sandbox disk")
                loaded[path] = data
            ws.files, ws.materialized = loaded, snapshot.snapshot_id
            return self._revision(ws, ())

    async def status(self, handle: SandboxHandleV1) -> WorkspaceRevisionV1:
        return self._revision(self._resolve(handle), ())

    async def list_files(
        self, handle: SandboxHandleV1, prefix: str = ".", limit: int = MAX_LIST_ENTRIES
    ) -> FileListingV1:
        ws = self._resolve(handle)
        facts = {p: (len(d), content_hash(d)) for p, d in ws.files.items()}
        return listing(facts, prefix, limit)

    async def read_file(
        self, handle: SandboxHandleV1, path: str, start_line: int = 1, end_line: int | None = None
    ) -> FileSliceV1:
        ws = self._resolve(handle)
        rel = normalize_workspace_path(path)
        data = ws.files.get(rel)
        if data is None:
            if any(p.startswith(rel + "/") for p in ws.files):
                raise NotATextFile("path is a directory")
            raise WorkspaceFileNotFound(rel)
        return text_slice(rel, data, start_line, end_line)

    async def file_hashes(self, handle: SandboxHandleV1) -> dict[str, str]:
        ws = self._resolve(handle)
        return {p: content_hash(d) for p, d in sorted(ws.files.items())}

    async def read_bytes(self, handle: SandboxHandleV1, path: str) -> bytes:
        ws = self._resolve(handle)
        rel = normalize_workspace_path(path)
        data = ws.files.get(rel)
        if data is None:
            if any(p.startswith(rel + "/") for p in ws.files):
                raise NotATextFile("path is a directory")
            raise WorkspaceFileNotFound(rel)
        return data

    async def write_files(
        self, handle: SandboxHandleV1, changes: tuple[FileChangeV1, ...]
    ) -> WorkspaceRevisionV1:
        ws = self._resolve(handle)
        if not changes or len(changes) > MAX_CHANGES_PER_CALL:
            raise SandboxLimit("a batch carries 1 to 200 changes")
        async with ws.lock:
            staged = dict(ws.files)
            touched: list[str] = []
            seen: set[str] = set()
            for change in changes:
                path = normalize_workspace_path(change.path)
                if path in seen:
                    raise PathRejected("a path appears once per batch")
                seen.add(path)
                if change.op == "write":
                    assert change.content is not None
                    if len(change.content) > MAX_FILE_BYTES:
                        raise SandboxLimit("file exceeds the size limit")
                    self._expect(staged, path, change.expected_hash)
                    staged[path] = change.content
                    touched.append(path)
                elif change.op == "delete":
                    self._expect(staged, path, change.expected_hash)
                    del staged[path]
                    touched.append(path)
                else:
                    assert change.move_to is not None
                    dest = normalize_workspace_path(change.move_to)
                    if dest in seen:
                        raise PathRejected("a path appears once per batch")
                    seen.add(dest)
                    self._expect(staged, path, change.expected_hash)
                    if dest in staged:
                        raise WorkspaceConflict(dest, None, content_hash(staged[dest]))
                    staged[dest] = staged.pop(path)
                    touched.extend((path, dest))
            if sum(len(c.content or b"") for c in changes) > MAX_BATCH_BYTES:
                raise SandboxLimit("batch exceeds the size limit")
            if len(staged) > MAX_WORKSPACE_FILES:
                raise SandboxLimit("workspace has too many files")
            if sum(len(v) for v in staged.values()) > ws.spec.disk_mib * 1024 * 1024:
                raise SandboxLimit("workspace exceeds the sandbox disk")
            ws.files = staged
            ws.revision += 1
            return self._revision(ws, tuple(dict.fromkeys(touched)))

    async def exec(
        self, handle: SandboxHandleV1, request: ExecRequestV1
    ) -> AsyncIterator[ExecChunkV1]:
        ws = self._resolve(handle)
        if request.timeout_s > ws.spec.command_timeout_s:
            raise ExecRejected("timeout exceeds the sandbox limit")
        cwd = normalize_workspace_path(request.cwd, allow_root=True)
        if cwd != "." and not any(p.startswith(cwd + "/") for p in ws.files):
            raise ExecRejected("working directory does not exist")
        self.exec_calls.append(request)
        self.cwd_calls.append(cwd)
        result = self._lookup(request, ws)
        started = time.monotonic()
        timed_out = result.delay_s > request.timeout_s
        await asyncio.sleep(min(result.delay_s, request.timeout_s))
        sent, truncated = 0, False
        for kind, text in (("stdout", result.stdout), ("stderr", result.stderr)):
            if not text:
                continue
            room = MAX_EXEC_OUTPUT_BYTES - sent
            data = text.encode()
            if len(data) > room:
                data, truncated = data[:room], True
            sent += len(data)
            yield ExecChunkV1(kind=kind, text=data.decode("utf-8", errors="ignore"))  # type: ignore[arg-type]
        if result.writes and not timed_out:
            async with ws.lock:
                for raw, data in result.writes.items():
                    ws.files[normalize_workspace_path(raw)] = data
                ws.revision += 1
        yield ExecChunkV1(
            kind="exit",
            exit_code=None if timed_out else result.exit_code,
            timed_out=timed_out,
            truncated=truncated,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    async def checkpoint(self, handle: SandboxHandleV1, label: str = "") -> CheckpointRefV1:
        ws = self._resolve(handle)
        async with ws.lock:
            ref = CheckpointRefV1(
                id=uuid4(),
                revision=ws.revision,
                tree_hash=self._tree(ws.files),
                label=label[:256],
            )
            ws.checkpoints[ref.id] = (ws.revision, dict(ws.files))
            return ref

    async def restore(
        self, handle: SandboxHandleV1, checkpoint: CheckpointRefV1
    ) -> WorkspaceRevisionV1:
        ws = self._resolve(handle)
        async with ws.lock:
            saved = ws.checkpoints.get(checkpoint.id)
            if saved is None or self._tree(saved[1]) != checkpoint.tree_hash:
                raise CheckpointUnknown("checkpoint does not belong to this sandbox")
            changed = {
                p for p in set(ws.files) | set(saved[1]) if ws.files.get(p) != saved[1].get(p)
            }
            ws.files = dict(saved[1])
            ws.revision += 1
            return self._revision(ws, tuple(sorted(changed)))

    async def suspend(self, handle: SandboxHandleV1) -> None:
        ws = self._resolve(handle, resume=False)
        if self._supports_suspend:
            ws.state = "suspended"

    async def release(self, handle: SandboxHandleV1) -> None:
        ws = self._by_handle.get(handle.id)
        if ws is None or ws.spec.org_id != handle.org_id or ws.spec.session_id != handle.session_id:
            raise SandboxGone("unknown sandbox")
        ws.state = "released"
        ws.files, ws.checkpoints = {}, {}

    # --- helpers -------------------------------------------------------------------------------

    def _resolve(self, handle: SandboxHandleV1, *, resume: bool = True) -> _Workspace:
        ws = self._by_handle.get(handle.id)
        if (
            ws is None
            or ws.spec.org_id != handle.org_id
            or ws.spec.session_id != handle.session_id
            or ws.state == "released"
        ):
            raise SandboxGone("unknown or released sandbox")
        if resume and ws.state == "suspended":
            ws.state = "running"
        return ws

    def _handle(self, ws: _Workspace) -> SandboxHandleV1:
        return SandboxHandleV1(
            id=ws.handle_id,
            org_id=ws.spec.org_id,
            session_id=ws.spec.session_id,
            state="suspended" if ws.state == "suspended" else "running",
        )

    @staticmethod
    def _tree(files: Mapping[str, bytes]) -> str:
        return tree_digest({p: content_hash(d) for p, d in files.items()})

    def _revision(self, ws: _Workspace, changed: tuple[str, ...]) -> WorkspaceRevisionV1:
        return WorkspaceRevisionV1(
            revision=ws.revision,
            tree_hash=self._tree(ws.files),
            file_count=len(ws.files),
            total_bytes=sum(len(v) for v in ws.files.values()),
            changed_paths=changed,
        )

    @staticmethod
    def _expect(files: Mapping[str, bytes], path: str, expected: str | None) -> None:
        current = files.get(path)
        actual = None if current is None else content_hash(current)
        if actual != expected:
            raise WorkspaceConflict(path, expected, actual)

    def _lookup(self, request: ExecRequestV1, ws: _Workspace) -> ScriptedExec:
        if self._default is not None:
            return self._default(request, ws.files)
        for prefix, result in self._scripts:
            if request.argv[: len(prefix)] == prefix:
                return result
        return ScriptedExec(stderr=f"{request.argv[0]}: command not found\n", exit_code=127)
