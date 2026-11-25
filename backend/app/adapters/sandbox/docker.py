"""Docker sandbox: a persistent workspace on the host, one short-lived container per command.

The workspace is a host directory bind-mounted at ``/workspace``. Files are read and written on the
host through ``HostTree``, which never follows a link a command planted. Each command runs in its
own container (read-only root, no network, no capabilities, bounded memory, CPU, processes and
file size) that is removed when the command ends, so nothing a command starts outlives it and a
fork bomb or a stuck process cannot starve later commands. gVisor (``runsc``) is used when the
Docker daemon has it. Checkpoints are content-addressed on the host.
"""

import asyncio
import codecs
import contextlib
import json
import os
import shutil
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID, uuid4

from app.adapters.sandbox.common import listing, text_slice
from app.adapters.sandbox.hosttree import FileFact, HostTree
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
    SandboxError,
    SandboxGone,
    SandboxLimit,
    SandboxStateError,
    SandboxUnavailable,
    SnapshotFilesPort,
    WorkspaceConflict,
    WorkspaceFileNotFound,
    normalize_workspace_path,
    tree_digest,
)

MIB = 1024 * 1024
DISK_POLL_SECONDS_MIN = 0.25
LABEL = "codespy.sandbox"


@dataclass
class _Sandbox:
    spec: SandboxSpecV1
    handle_id: UUID
    dir: Path
    tree: HostTree
    revision: int = 0
    materialized: UUID | None = None
    tree_hash: str = ""
    checkpoints: dict[UUID, tuple[int, str]] = field(default_factory=dict)
    released: bool = False
    running: set[str] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class DockerSandbox:
    def __init__(
        self,
        root: Path,
        snapshots: SnapshotFilesPort,
        *,
        image: str,
        allowed_images: frozenset[str] | None = None,
        docker_bin: str = "docker",
        marker: str = "",
        user: str | None = None,
        disk_quota_bytes: int | None = None,
    ) -> None:
        """``root`` holds every sandbox workspace and its state; Docker must be able to mount it."""
        self._root = root.resolve() if root.exists() else root
        self._snapshots = snapshots
        self._image = image
        self._allowed = allowed_images or frozenset({image})
        self._docker = docker_bin
        self._marker = marker
        self._user = user or f"{os.getuid()}:{os.getgid()}"
        self._quota_override = disk_quota_bytes
        self._live: dict[tuple[UUID, UUID], _Sandbox] = {}
        self._by_handle: dict[UUID, _Sandbox] = {}
        self._acquire_lock = asyncio.Lock()
        self._checked: set[str] = set()
        self._runtime: str | None = None
        self._isolation: str = "container"
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._root = root.resolve()

    # --- port ----------------------------------------------------------------------------------

    @property
    def capabilities(self) -> SandboxCapabilitiesV1:
        return SandboxCapabilitiesV1(
            supports_suspend=False,
            isolation=self._isolation,  # type: ignore[arg-type]
            network_egress="none",
        )

    async def acquire(self, spec: SandboxSpecV1) -> SandboxHandleV1:
        if spec.image not in self._allowed:
            raise SandboxStateError("image is not allowed")
        if spec.egress != "none":
            raise SandboxStateError("this sandbox has no network egress")
        async with self._acquire_lock:
            await self._check_runtime(spec.image)
            key = (spec.org_id, spec.session_id)
            sbx = self._live.get(key)
            if sbx is None or sbx.released:
                sbx = await asyncio.to_thread(self._open, spec)
                self._live[key] = sbx
                self._by_handle[sbx.handle_id] = sbx
            return self._handle(sbx)

    async def materialize(
        self, handle: SandboxHandleV1, snapshot: SnapshotRefV1
    ) -> WorkspaceRevisionV1:
        sbx = self._resolve(handle)
        if snapshot.org_id != sbx.spec.org_id:
            raise SandboxGone("snapshot belongs to another organisation")
        async with sbx.lock:
            if sbx.materialized is not None:
                if sbx.materialized != snapshot.snapshot_id:
                    raise SandboxStateError("sandbox already holds another snapshot")
                return await self._view(sbx, ())
            quota, count, total = self._quota(sbx), 0, 0
            try:
                async for raw_path, data in self._snapshots.files(snapshot):
                    path = normalize_workspace_path(raw_path)
                    count, total = count + 1, total + len(data)
                    if count > MAX_WORKSPACE_FILES or len(data) > MAX_FILE_BYTES:
                        raise SandboxLimit("snapshot exceeds workspace limits")
                    if total > quota:
                        raise SandboxLimit("snapshot exceeds the sandbox disk")
                    await asyncio.to_thread(sbx.tree.write, path, data)
            except BaseException:
                await asyncio.to_thread(sbx.tree.clear)
                raise
            sbx.materialized = snapshot.snapshot_id
            view = await self._view(sbx, ())
            self._save(sbx)
            return view

    async def status(self, handle: SandboxHandleV1) -> WorkspaceRevisionV1:
        sbx = self._resolve(handle)
        async with sbx.lock:
            return await self._view(sbx, ())

    async def list_files(
        self, handle: SandboxHandleV1, prefix: str = ".", limit: int = MAX_LIST_ENTRIES
    ) -> FileListingV1:
        sbx = self._resolve(handle)
        facts = await asyncio.to_thread(sbx.tree.facts)
        return listing({p: (f.size, f.sha256) for p, f in facts.items()}, prefix, limit)

    async def read_file(
        self, handle: SandboxHandleV1, path: str, start_line: int = 1, end_line: int | None = None
    ) -> FileSliceV1:
        sbx = self._resolve(handle)
        rel = normalize_workspace_path(path)
        kind = await asyncio.to_thread(sbx.tree.kind, rel)
        if kind is None:
            raise WorkspaceFileNotFound(rel)
        if kind != "file":
            raise NotATextFile("path is not a regular file")
        data = await asyncio.to_thread(sbx.tree.read, rel)
        if data is None:
            raise WorkspaceFileNotFound(rel)
        return text_slice(rel, data, start_line, end_line)

    async def file_hashes(self, handle: SandboxHandleV1) -> dict[str, str]:
        sbx = self._resolve(handle)
        facts = await asyncio.to_thread(sbx.tree.facts)
        return {p: f.sha256 for p, f in facts.items()}

    async def read_bytes(self, handle: SandboxHandleV1, path: str) -> bytes:
        sbx = self._resolve(handle)
        rel = normalize_workspace_path(path)
        kind = await asyncio.to_thread(sbx.tree.kind, rel)
        if kind is None:
            raise WorkspaceFileNotFound(rel)
        if kind != "file":
            raise NotATextFile("path is not a regular file")
        data = await asyncio.to_thread(sbx.tree.read, rel)
        if data is None:
            raise WorkspaceFileNotFound(rel)
        return data

    async def write_files(
        self, handle: SandboxHandleV1, changes: tuple[FileChangeV1, ...]
    ) -> WorkspaceRevisionV1:
        sbx = self._resolve(handle)
        if not changes or len(changes) > MAX_CHANGES_PER_CALL:
            raise SandboxLimit("a batch carries 1 to 200 changes")
        async with sbx.lock:
            return await asyncio.to_thread(self._write_sync, sbx, changes) or await self._view(
                sbx, ()
            )

    def exec(self, handle: SandboxHandleV1, request: ExecRequestV1) -> AsyncIterator[ExecChunkV1]:
        return self._exec(handle, request)

    async def checkpoint(self, handle: SandboxHandleV1, label: str = "") -> CheckpointRefV1:
        sbx = self._resolve(handle)
        async with sbx.lock:
            await self._sync_revision(sbx)
            facts = await asyncio.to_thread(sbx.tree.facts)
            cid = uuid4()
            await asyncio.to_thread(self._store_checkpoint, sbx, cid, facts)
            sbx.checkpoints[cid] = (sbx.revision, sbx.tree_hash)
            self._save(sbx)
            return CheckpointRefV1(
                id=cid, revision=sbx.revision, tree_hash=sbx.tree_hash, label=label[:256]
            )

    async def restore(
        self, handle: SandboxHandleV1, checkpoint: CheckpointRefV1
    ) -> WorkspaceRevisionV1:
        sbx = self._resolve(handle)
        async with sbx.lock:
            saved = sbx.checkpoints.get(checkpoint.id)
            if saved is None or saved[1] != checkpoint.tree_hash:
                raise CheckpointUnknown("checkpoint does not belong to this sandbox")
            changed = await asyncio.to_thread(self._restore_sync, sbx, checkpoint.id)
            sbx.revision += 1
            view = await self._view(sbx, changed)
            if view.tree_hash != checkpoint.tree_hash:
                raise SandboxError("restored workspace differs from the checkpoint")
            self._save(sbx)
            return view

    async def suspend(self, handle: SandboxHandleV1) -> None:
        self._resolve(handle)  # nothing runs between commands, so there is nothing to suspend

    async def release(self, handle: SandboxHandleV1) -> None:
        sbx = self._by_handle.get(handle.id)
        if sbx is None or (sbx.spec.org_id, sbx.spec.session_id) != (
            handle.org_id,
            handle.session_id,
        ):
            raise SandboxGone("unknown sandbox")
        if sbx.released:
            return
        sbx.released = True
        for name in list(sbx.running):
            await self._run_docker("kill", name, check=False)
        await asyncio.to_thread(self._destroy, sbx)

    async def reap(self) -> None:
        """Remove containers this adapter's marker started and did not finish (tests, crashes)."""
        if not self._marker:
            return
        _, out, _ = await self._run_docker(
            "ps", "-aq", "--filter", f"label={LABEL}.marker={self._marker}", check=False
        )
        ids = out.decode().split()
        if ids:
            await self._run_docker("rm", "-f", *ids, check=False)

    # --- exec ----------------------------------------------------------------------------------

    async def _exec(
        self, handle: SandboxHandleV1, request: ExecRequestV1
    ) -> AsyncIterator[ExecChunkV1]:
        sbx = self._resolve(handle)
        if request.timeout_s > sbx.spec.command_timeout_s:
            raise ExecRejected("timeout exceeds the sandbox limit")
        cwd = normalize_workspace_path(request.cwd, allow_root=True)
        if not await asyncio.to_thread(sbx.tree.dir_exists, cwd):
            raise ExecRejected("working directory does not exist")
        name = "codespy-sbx-" + uuid4().hex
        sbx.running.add(name)
        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            self._docker,
            *self._run_args(sbx, name, request, cwd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert proc.stdout is not None and proc.stderr is not None
        queue: asyncio.Queue[tuple[str, bytes]] = asyncio.Queue()
        verdict: dict[str, str] = {}

        async def pump(stream: asyncio.StreamReader, kind: str) -> None:
            while data := await stream.read(8192):
                await queue.put((kind, data))
            await queue.put((kind, b""))

        pumps = [
            asyncio.create_task(pump(proc.stdout, "stdout")),
            asyncio.create_task(pump(proc.stderr, "stderr")),
        ]
        baseline = await asyncio.to_thread(self._safe_total, sbx)
        watchdog = asyncio.create_task(self._watch(sbx, name, request.timeout_s, baseline, verdict))
        decoders = {
            "stdout": codecs.getincrementaldecoder("utf-8")(errors="replace"),
            "stderr": codecs.getincrementaldecoder("utf-8")(errors="replace"),
        }
        kept, truncated, diagnostics, open_streams = 0, False, bytearray(), 2
        try:
            while open_streams:
                kind, data = await queue.get()
                if not data:
                    open_streams -= 1
                    continue
                if kind == "stderr" and len(diagnostics) < 4096:
                    diagnostics.extend(data[: 4096 - len(diagnostics)])
                room = MAX_EXEC_OUTPUT_BYTES - kept
                if len(data) > room:
                    data, truncated = data[: max(room, 0)], True
                kept += len(data)
                text = decoders[kind].decode(data)
                if text:
                    yield ExecChunkV1(kind=kind, text=text)  # type: ignore[arg-type]
            code = await proc.wait()
        finally:
            watchdog.cancel()
            for task in pumps:
                task.cancel()
            if proc.returncode is None:
                await self._run_docker("kill", name, check=False)
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
            sbx.running.discard(name)
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await watchdog
        if code == 125 and diagnostics.startswith(b"docker:"):
            raise SandboxUnavailable("the container runtime rejected the command")
        async with sbx.lock:
            await self._sync_revision(sbx)
        timed_out = verdict.get("why") == "timeout"
        grew_past_quota = (
            not verdict
            and (after := await asyncio.to_thread(self._safe_total, sbx)) > self._quota(sbx)
            and after > baseline
        )
        yield ExecChunkV1(
            kind="exit",
            exit_code=None if verdict else code,
            timed_out=timed_out,
            truncated=truncated,
            limit_exceeded="disk" if verdict.get("why") == "disk" or grew_past_quota else None,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    async def _watch(
        self,
        sbx: _Sandbox,
        name: str,
        timeout_s: int,
        baseline: int,
        verdict: dict[str, str],
    ) -> None:
        """Stop the command when its time runs out or it grows the workspace past the quota.

        A command that is only deleting (the workspace is over quota and shrinking) is left alone,
        so an agent can always clean up. The poll interval adapts to how long a measurement takes.
        """
        deadline = time.monotonic() + timeout_s
        quota = self._quota(sbx)
        last, pause = baseline, DISK_POLL_SECONDS_MIN
        while True:
            await asyncio.sleep(min(pause, max(deadline - time.monotonic(), 0.01)))
            if time.monotonic() >= deadline:
                verdict["why"] = "timeout"
            else:
                began = time.monotonic()
                try:
                    size = await asyncio.to_thread(sbx.tree.total_bytes)
                except SandboxError:
                    size = quota + 1  # too many files to count is also over the limit
                if size > quota and size > last:
                    verdict["why"] = "disk"
                last = size
                pause = min(max(DISK_POLL_SECONDS_MIN, 4 * (time.monotonic() - began)), 2.0)
            if verdict:
                await self._run_docker("kill", name, check=False)
                return

    def _run_args(self, sbx: _Sandbox, name: str, request: ExecRequestV1, cwd: str) -> list[str]:
        spec = sbx.spec
        uid, _, gid = self._user.partition(":")
        quota = self._quota(sbx)
        args = [
            "run",
            "--rm",
            "--init",
            "--name",
            name,
            "--label",
            f"{LABEL}=1",
            "--label",
            f"{LABEL}.marker={self._marker}",
            "--label",
            f"{LABEL}.org={spec.org_id}",
            "--label",
            f"{LABEL}.session={spec.session_id}",
            "--network",
            "none",
            "--read-only",
            "--user",
            self._user,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--memory",
            f"{spec.memory_mib}m",
            "--memory-swap",
            f"{spec.memory_mib}m",
            "--cpus",
            str(spec.cpu),
            "--pids-limit",
            str(spec.pids),
            "--ulimit",
            "nofile=1024:1024",
            "--ulimit",
            "core=0",
            "--ulimit",
            f"fsize={quota}:{quota}",
            "--log-driver",
            "none",
            "--tmpfs",
            "/tmp:rw,nosuid,noexec,size=268435456,mode=1777",  # noqa: S108 - container tmpfs
            "--tmpfs",
            f"/home/sandbox:rw,nosuid,noexec,size=134217728,mode=700,uid={uid},gid={gid or uid}",
            "--env",
            "HOME=/home/sandbox",
            "--env",
            "TMPDIR=/tmp",
            "--mount",
            f"type=bind,src={sbx.tree.root},dst=/workspace",
            "--workdir",
            "/workspace" if cwd == "." else f"/workspace/{cwd}",
        ]
        if self._runtime:
            args += ["--runtime", self._runtime]
        for key, value in request.env.items():
            args += ["--env", f"{key}={value}"]
        return [*args, sbx.spec.image, *request.argv]

    # --- internals -----------------------------------------------------------------------------

    @staticmethod
    def _safe_total(sbx: _Sandbox) -> int:
        try:
            return sbx.tree.total_bytes()
        except SandboxError:
            return 1 << 62

    def _quota(self, sbx: _Sandbox) -> int:
        return self._quota_override or sbx.spec.disk_mib * MIB

    def _handle(self, sbx: _Sandbox) -> SandboxHandleV1:
        return SandboxHandleV1(
            id=sbx.handle_id,
            org_id=sbx.spec.org_id,
            session_id=sbx.spec.session_id,
            state="running",
        )

    def _resolve(self, handle: SandboxHandleV1) -> _Sandbox:
        sbx = self._by_handle.get(handle.id)
        if (
            sbx is None
            or sbx.released
            or (sbx.spec.org_id, sbx.spec.session_id) != (handle.org_id, handle.session_id)
        ):
            raise SandboxGone("unknown or released sandbox")
        return sbx

    def _open(self, spec: SandboxSpecV1) -> _Sandbox:
        directory = self._root / str(spec.org_id) / str(spec.session_id)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        sbx = _Sandbox(
            spec=spec,
            handle_id=uuid4(),
            dir=directory,
            tree=HostTree(directory / "workspace"),
        )
        state = directory / "state.json"
        if state.is_file():  # a restarted process re-attaches to the same workspace
            data = json.loads(state.read_text())
            sbx.handle_id = UUID(data["handle_id"])
            sbx.revision = data["revision"]
            sbx.materialized = UUID(data["materialized"]) if data["materialized"] else None
            sbx.tree_hash = data["tree_hash"]
            sbx.checkpoints = {
                UUID(k): (v["revision"], v["tree_hash"]) for k, v in data["checkpoints"].items()
            }
        else:
            sbx.tree_hash = tree_digest({})
            self._save(sbx)
        return sbx

    def _save(self, sbx: _Sandbox) -> None:
        data = {
            "handle_id": str(sbx.handle_id),
            "revision": sbx.revision,
            "materialized": str(sbx.materialized) if sbx.materialized else None,
            "tree_hash": sbx.tree_hash,
            "checkpoints": {
                str(k): {"revision": r, "tree_hash": h} for k, (r, h) in sbx.checkpoints.items()
            },
        }
        tmp = sbx.dir / "state.json.tmp"
        tmp.write_text(json.dumps(data))
        tmp.replace(sbx.dir / "state.json")

    def _destroy(self, sbx: _Sandbox) -> None:
        sbx.tree.clear()
        shutil.rmtree(sbx.dir, ignore_errors=True)
        self._live.pop((sbx.spec.org_id, sbx.spec.session_id), None)

    async def _view(self, sbx: _Sandbox, changed: tuple[str, ...]) -> WorkspaceRevisionV1:
        facts = await asyncio.to_thread(sbx.tree.facts)
        digest = tree_digest({p: f.sha256 for p, f in facts.items()})
        sbx.tree_hash = digest
        return WorkspaceRevisionV1(
            revision=sbx.revision,
            tree_hash=digest,
            file_count=len(facts),
            total_bytes=sum(f.size for f in facts.values()),
            changed_paths=changed[:1000],
        )

    async def _sync_revision(self, sbx: _Sandbox) -> None:
        """Advance the revision once when a command changed the workspace. Call under the lock."""
        facts = await asyncio.to_thread(sbx.tree.facts)
        digest = tree_digest({p: f.sha256 for p, f in facts.items()})
        if digest != sbx.tree_hash:
            sbx.revision += 1
            sbx.tree_hash = digest
            self._save(sbx)

    def _write_sync(
        self, sbx: _Sandbox, changes: tuple[FileChangeV1, ...]
    ) -> WorkspaceRevisionV1 | None:
        tree = sbx.tree
        facts = tree.facts()
        seen: set[str] = set()
        ops: list[tuple[str, str, bytes | None, str | None]] = []
        incoming = delta = 0
        for change in changes:
            path = normalize_workspace_path(change.path)
            if path in seen:
                raise PathRejected("a path appears once per batch")
            seen.add(path)
            actual = facts[path].sha256 if path in facts else None
            if change.op == "write":
                assert change.content is not None
                if len(change.content) > MAX_FILE_BYTES:
                    raise SandboxLimit("file exceeds the size limit")
                if actual != change.expected_hash:
                    raise WorkspaceConflict(path, change.expected_hash, actual)
                if tree.kind(path) == "dir":
                    raise PathRejected("path is a directory")
                incoming += len(change.content)
                delta += len(change.content) - (facts[path].size if path in facts else 0)
                ops.append(("write", path, change.content, None))
            elif change.op == "delete":
                if actual != change.expected_hash:
                    raise WorkspaceConflict(path, change.expected_hash, actual)
                delta -= facts[path].size
                ops.append(("delete", path, None, None))
            else:
                assert change.move_to is not None
                dest = normalize_workspace_path(change.move_to)
                if dest in seen:
                    raise PathRejected("a path appears once per batch")
                seen.add(dest)
                if actual != change.expected_hash:
                    raise WorkspaceConflict(path, change.expected_hash, actual)
                if dest in facts:
                    raise WorkspaceConflict(dest, None, facts[dest].sha256)
                if tree.kind(dest) == "dir":
                    raise PathRejected("path is a directory")
                ops.append(("move", path, None, dest))
        if incoming > MAX_BATCH_BYTES:
            raise SandboxLimit("batch exceeds the size limit")
        if delta > 0 and sum(f.size for f in facts.values()) + delta > self._quota(sbx):
            raise SandboxLimit("workspace exceeds the sandbox disk")
        if len(facts) + len(ops) > MAX_WORKSPACE_FILES:
            raise SandboxLimit("workspace has too many files")
        backups: dict[str, bytes | None] = {}
        for _kind, src, _payload, dst in ops:
            for target in (src, dst):
                if target is not None and target not in backups:
                    backups[target] = tree.read(target) if target in facts else None
        done: list[tuple[str, str, str | None]] = []
        try:
            for kind, src, payload, dst in ops:
                if kind == "write":
                    assert payload is not None
                    tree.write(src, payload)
                elif kind == "delete":
                    tree.delete(src)
                else:
                    assert dst is not None
                    tree.move(src, dst)
                done.append((kind, src, dst))
        except BaseException:
            for kind, src, dst in reversed(done):  # best-effort rollback keeps the batch atomic
                with contextlib.suppress(Exception):
                    saved = backups.get(src)
                    if kind == "move" and dst is not None:
                        tree.move(dst, src)
                    elif saved is None:
                        tree.delete(src)
                    else:
                        tree.write(src, saved)
            raise
        touched: list[str] = []
        for _kind, src, _payload, dst in ops:
            touched.extend([src, dst] if dst else [src])
        sbx.revision += 1
        after = tree.facts()
        digest = tree_digest({p: f.sha256 for p, f in after.items()})
        sbx.tree_hash = digest
        self._save(sbx)
        return WorkspaceRevisionV1(
            revision=sbx.revision,
            tree_hash=digest,
            file_count=len(after),
            total_bytes=sum(f.size for f in after.values()),
            changed_paths=tuple(dict.fromkeys(touched))[:1000],
        )

    def _blob(self, sbx: _Sandbox, digest: str) -> Path:
        return sbx.dir / "blobs" / digest

    def _store_checkpoint(self, sbx: _Sandbox, cid: UUID, facts: Mapping[str, FileFact]) -> None:
        (sbx.dir / "blobs").mkdir(exist_ok=True, mode=0o700)
        (sbx.dir / "checkpoints").mkdir(exist_ok=True, mode=0o700)
        for rel, fact in facts.items():
            blob = self._blob(sbx, fact.sha256)
            if not blob.exists():
                partial = blob.with_suffix(".part")
                sbx.tree.export(rel, partial)
                partial.replace(blob)
        manifest = {rel: {"sha256": f.sha256, "mode": f.mode} for rel, f in facts.items()}
        (sbx.dir / "checkpoints" / f"{cid}.json").write_text(json.dumps(manifest))

    def _restore_sync(self, sbx: _Sandbox, cid: UUID) -> tuple[str, ...]:
        manifest = json.loads((sbx.dir / "checkpoints" / f"{cid}.json").read_text())
        current = sbx.tree.facts()
        for entry in manifest.values():
            if not self._blob(sbx, entry["sha256"]).is_file():
                raise CheckpointUnknown("checkpoint content is missing")
        changed: list[str] = []
        for rel, entry in manifest.items():
            have = current.get(rel)
            if have is None or have.sha256 != entry["sha256"]:
                sbx.tree.import_file(rel, self._blob(sbx, entry["sha256"]), entry["mode"])
                changed.append(rel)
        for rel in current:
            if rel not in manifest:
                sbx.tree.delete(rel)
                changed.append(rel)
        return tuple(sorted(changed))

    async def _check_runtime(self, image: str) -> None:
        if image not in self._checked:
            code, _, _ = await self._run_docker("image", "inspect", image, check=False)
            if code != 0:
                raise SandboxUnavailable("sandbox image is not available; build it first")
            self._checked.add(image)
        if self._runtime is None:
            _, out, _ = await self._run_docker(
                "info", "--format", "{{json .Runtimes}}", check=False
            )
            if b"runsc" in out:
                self._runtime, self._isolation = "runsc", "gvisor"
            else:
                self._runtime = ""

    async def _run_docker(self, *args: str, check: bool = True) -> tuple[int, bytes, bytes]:
        try:
            proc = await asyncio.create_subprocess_exec(
                self._docker,
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(proc.communicate(), 60)
        except (FileNotFoundError, TimeoutError, OSError):
            if check:
                raise SandboxUnavailable("the container runtime is not reachable") from None
            return 1, b"", b""
        code = proc.returncode or 0
        if check and code != 0:
            raise SandboxUnavailable("the container runtime rejected the request")
        return code, out, err
