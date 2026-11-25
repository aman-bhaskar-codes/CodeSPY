"""The contract every ``SandboxPort`` implementation must pass.

A concrete test class subclasses ``SandboxContract`` and supplies ``build`` (the sandbox over the
given snapshots) and ``cmd`` (an argv whose behaviour is the one asked for: scripted on the fake,
a real shell command on a real sandbox). The suite never inspects an implementation's internals.
"""

import asyncio
from dataclasses import dataclass
from typing import ClassVar
from uuid import UUID, uuid4

import pytest
from app.contracts.workspace import (
    ExecRequestV1,
    FileChangeV1,
    SandboxHandleV1,
    SandboxSpecV1,
    SnapshotRefV1,
)
from app.ports.sandbox import (
    MAX_CHANGES_PER_CALL,
    MAX_EXEC_OUTPUT_BYTES,
    MAX_FILE_BYTES,
    MAX_READ_LINES,
    CheckpointUnknown,
    ExecRejected,
    NotATextFile,
    PathRejected,
    SandboxGone,
    SandboxLimit,
    SandboxPort,
    SandboxStateError,
    WorkspaceConflict,
    WorkspaceFileNotFound,
    content_hash,
)
from pydantic import ValidationError

SNAPSHOT = UUID("20000000-0000-4000-8000-000000000001")
OTHER_SNAPSHOT = UUID("20000000-0000-4000-8000-000000000002")
BIG = "".join(f"line {i}\n" for i in range(1, 3001)).encode()
FILES = {
    "README.md": b"# demo\n",
    "src/app.py": b"def main():\n    return 1\n",
    "src/util.py": b"X = 1\n",
    "big.txt": BIG,
    "data.bin": b"\x00\x01\x02\xff",
}


@dataclass
class Env:
    sandbox: SandboxPort
    org: UUID
    repo: UUID
    session: UUID
    handle: SandboxHandleV1
    ref: SnapshotRefV1

    def spec(self, session: UUID | None = None, **kw) -> SandboxSpecV1:
        return SandboxSpecV1(
            org_id=self.org, session_id=session or self.session, image="test:latest", **kw
        )


def write(path, content, expected=None):
    return FileChangeV1(op="write", path=path, content=content, expected_hash=expected)


def sha(data: bytes) -> str:
    return content_hash(data)


async def run(sandbox, handle, argv, **kw):
    chunks = [c async for c in sandbox.exec(handle, ExecRequestV1(argv=argv, **kw))]
    return chunks


class SandboxContract:
    async def build(self, snapshots: dict[UUID, dict[str, bytes]]) -> SandboxPort:
        raise NotImplementedError

    def cmd(
        self, sandbox, *, stdout="", stderr="", exit_code=0, delay_s=0.0, stdout_bytes=0
    ) -> tuple[str, ...]:
        raise NotImplementedError

    @pytest.fixture
    async def env(self) -> Env:
        sandbox = await self.build({SNAPSHOT: FILES, OTHER_SNAPSHOT: {"other.txt": b"o\n"}})
        org, repo, session = uuid4(), uuid4(), uuid4()
        probe = Env(sandbox, org, repo, session, None, None)  # type: ignore[arg-type]
        handle = await sandbox.acquire(probe.spec())
        ref = SnapshotRefV1(org_id=org, repository_id=repo, snapshot_id=SNAPSHOT)
        await sandbox.materialize(handle, ref)
        return Env(sandbox, org, repo, session, handle, ref)

    # --- identity and isolation ----------------------------------------------------------------

    async def test_acquire_attaches_to_a_live_sandbox_and_isolates_sessions(self, env: Env):
        again = await env.sandbox.acquire(env.spec())
        assert again.id == env.handle.id  # a restarted worker re-attaches
        other = await env.sandbox.acquire(env.spec(session=uuid4()))
        assert other.id != env.handle.id
        await env.sandbox.materialize(other, env.ref)
        await env.sandbox.write_files(other, (write("only-in-other.txt", b"x"),))
        assert [
            e.path
            for e in (await env.sandbox.list_files(env.handle)).entries
            if e.path == "only-in-other.txt"
        ] == []

    async def test_a_handle_is_not_authority(self, env: Env):
        forged = [
            env.handle.model_copy(update={"id": uuid4()}),
            env.handle.model_copy(update={"org_id": uuid4()}),
            env.handle.model_copy(update={"session_id": uuid4()}),
        ]
        for handle in forged:
            for call in (
                env.sandbox.status(handle),
                env.sandbox.read_file(handle, "README.md"),
                env.sandbox.write_files(handle, (write("x.txt", b"x"),)),
                env.sandbox.release(handle),
            ):
                with pytest.raises(SandboxGone):
                    await call

    async def test_release_is_terminal_and_a_new_acquire_is_a_fresh_empty_sandbox(self, env: Env):
        await env.sandbox.write_files(env.handle, (write("scratch.txt", b"s"),))
        await env.sandbox.release(env.handle)
        await env.sandbox.release(env.handle)  # idempotent
        with pytest.raises(SandboxGone):
            await env.sandbox.read_file(env.handle, "README.md")
        fresh = await env.sandbox.acquire(env.spec())
        assert fresh.id != env.handle.id
        assert (await env.sandbox.status(fresh)).file_count == 0  # nothing carried over

    # --- materialize, list, read ---------------------------------------------------------------

    async def test_materialize_loads_one_snapshot_once(self, env: Env):
        first = await env.sandbox.status(env.handle)
        assert first.revision == 0 and first.file_count == len(FILES)
        assert (await env.sandbox.materialize(env.handle, env.ref)).tree_hash == first.tree_hash
        with pytest.raises(SandboxStateError):
            await env.sandbox.materialize(
                env.handle, env.ref.model_copy(update={"snapshot_id": OTHER_SNAPSHOT})
            )
        with pytest.raises(SandboxGone):
            fresh = await env.sandbox.acquire(env.spec(session=uuid4()))
            await env.sandbox.materialize(fresh, env.ref.model_copy(update={"org_id": uuid4()}))

    async def test_list_files_is_sorted_hashed_and_bounded(self, env: Env):
        listing = await env.sandbox.list_files(env.handle)
        assert [e.path for e in listing.entries] == sorted(FILES) and not listing.truncated
        entry = next(e for e in listing.entries if e.path == "src/app.py")
        assert entry.size == len(FILES["src/app.py"]) and entry.file_hash == sha(
            FILES["src/app.py"]
        )
        assert [e.path for e in (await env.sandbox.list_files(env.handle, "src")).entries] == [
            "src/app.py",
            "src/util.py",
        ]
        assert [
            e.path for e in (await env.sandbox.list_files(env.handle, "sr")).entries
        ] == []  # a prefix is a directory, not a string
        small = await env.sandbox.list_files(env.handle, limit=2)
        assert len(small.entries) == 2 and small.truncated

    async def test_read_file_returns_lines_hash_and_totals(self, env: Env):
        whole = await env.sandbox.read_file(env.handle, "src/app.py")
        assert whole.content == FILES["src/app.py"].decode() and whole.total_lines == 2
        assert whole.file_hash == sha(FILES["src/app.py"]) and not whole.truncated
        part = await env.sandbox.read_file(env.handle, "big.txt", 10, 12)
        assert part.content == "line 10\nline 11\nline 12\n" and (
            part.start_line,
            part.end_line,
        ) == (10, 12)
        assert part.total_lines == 3000 and part.file_hash == sha(BIG)
        clamped = await env.sandbox.read_file(env.handle, "src/app.py", 2, 99)
        assert clamped.content == "    return 1\n" and clamped.end_line == 2
        beyond = await env.sandbox.read_file(env.handle, "src/app.py", 50)
        assert beyond.content == "" and beyond.total_lines == 2

    async def test_read_file_is_bounded_and_rejects_non_text(self, env: Env):
        capped = await env.sandbox.read_file(env.handle, "big.txt", 1, 3000)
        assert (
            capped.truncated
            and capped.content.count("\n") <= MAX_READ_LINES
            and capped.end_line <= MAX_READ_LINES
        )
        with pytest.raises(WorkspaceFileNotFound):
            await env.sandbox.read_file(env.handle, "nope.txt")
        with pytest.raises(NotATextFile):
            await env.sandbox.read_file(env.handle, "src")
        with pytest.raises(NotATextFile):
            await env.sandbox.read_file(env.handle, "data.bin")
        for bad in ((0, None), (5, 2)):
            with pytest.raises(PathRejected):
                await env.sandbox.read_file(env.handle, "README.md", *bad)

    # --- paths ---------------------------------------------------------------------------------

    BAD_PATHS: ClassVar[tuple[str, ...]] = (
        "",
        "/etc/passwd",
        "../x",
        "a/../../x",
        "a//b",
        "a/./b",
        "a/",
        "a\\b",
        ".git/config",
        "src/.GIT/x",
        "x\x00y",
        "x\ny",
        "a" * 300,
        "/".join(["d"] * 70),
    )

    @pytest.mark.parametrize("bad", BAD_PATHS)
    async def test_unsafe_paths_are_rejected_on_every_call(self, env: Env, bad):
        sb, h = env.sandbox, env.handle

        def move(dest):
            return FileChangeV1(
                op="move", path="README.md", expected_hash=sha(FILES["README.md"]), move_to=dest
            )

        calls = (
            lambda: sb.read_file(h, bad),
            lambda: sb.write_files(h, (write(bad, b"x"),)),
            lambda: sb.write_files(h, (move(bad),)),
            lambda: sb.list_files(h, bad),
        )
        for call in calls:
            # The contract may refuse the path before the port sees it; either way nothing runs.
            with pytest.raises((PathRejected, ValidationError)):
                await call()
        assert (await sb.status(h)).revision == 0  # nothing was applied

    async def test_a_leading_dot_slash_is_normalised(self, env: Env):
        assert (await env.sandbox.read_file(env.handle, "./README.md")).path == "README.md"

    # --- writes --------------------------------------------------------------------------------

    async def test_create_modify_move_and_delete_advance_one_revision_per_call(self, env: Env):
        base = await env.sandbox.status(env.handle)
        created = await env.sandbox.write_files(env.handle, (write("new.txt", b"hello\n"),))
        assert created.revision == base.revision + 1 and created.changed_paths == ("new.txt",)
        assert created.tree_hash != base.tree_hash and created.file_count == base.file_count + 1
        edited = await env.sandbox.write_files(
            env.handle, (write("new.txt", b"hello world\n", sha(b"hello\n")),)
        )
        assert edited.revision == created.revision + 1
        moved = await env.sandbox.write_files(
            env.handle,
            (
                FileChangeV1(
                    op="move",
                    path="new.txt",
                    expected_hash=sha(b"hello world\n"),
                    move_to="docs/new.txt",
                ),
            ),
        )
        assert set(moved.changed_paths) == {"new.txt", "docs/new.txt"}
        assert (await env.sandbox.read_file(env.handle, "docs/new.txt")).content == "hello world\n"
        with pytest.raises(WorkspaceFileNotFound):
            await env.sandbox.read_file(env.handle, "new.txt")
        deleted = await env.sandbox.write_files(
            env.handle,
            (FileChangeV1(op="delete", path="docs/new.txt", expected_hash=sha(b"hello world\n")),),
        )
        assert (
            deleted.tree_hash == base.tree_hash and deleted.revision == moved.revision + 1
        )  # content-addressed

    async def test_a_batch_is_all_or_nothing(self, env: Env):
        before = await env.sandbox.status(env.handle)
        with pytest.raises(WorkspaceConflict) as conflict:
            await env.sandbox.write_files(
                env.handle,
                (
                    write("ok.txt", b"fine"),
                    write("src/app.py", b"changed", expected=sha(b"not what is there")),
                ),
            )
        assert conflict.value.path == "src/app.py" and conflict.value.actual == sha(
            FILES["src/app.py"]
        )
        assert conflict.value.expected == sha(b"not what is there")
        after = await env.sandbox.status(env.handle)
        assert (after.revision, after.tree_hash) == (before.revision, before.tree_hash)
        with pytest.raises(WorkspaceFileNotFound):
            await env.sandbox.read_file(env.handle, "ok.txt")

    async def test_conflicts_cover_create_over_existing_and_move_onto_existing(self, env: Env):
        with pytest.raises(WorkspaceConflict):  # a create names no hash but the path exists
            await env.sandbox.write_files(env.handle, (write("README.md", b"x"),))
        with pytest.raises(WorkspaceConflict):  # modify a path that does not exist
            await env.sandbox.write_files(
                env.handle, (write("ghost.txt", b"x", expected=sha(b"y")),)
            )
        with pytest.raises(WorkspaceConflict):
            await env.sandbox.write_files(
                env.handle,
                (
                    FileChangeV1(
                        op="move",
                        path="README.md",
                        expected_hash=sha(FILES["README.md"]),
                        move_to="src/app.py",
                    ),
                ),
            )
        with pytest.raises(WorkspaceConflict):
            await env.sandbox.write_files(
                env.handle,
                (FileChangeV1(op="delete", path="README.md", expected_hash=sha(b"stale")),),
            )

    async def test_batch_shape_rules(self, env: Env):
        for bad in ((), tuple(write(f"f{i}.txt", b"x") for i in range(MAX_CHANGES_PER_CALL + 1))):
            with pytest.raises(SandboxLimit):
                await env.sandbox.write_files(env.handle, bad)
        with pytest.raises(PathRejected):
            await env.sandbox.write_files(
                env.handle, (write("same.txt", b"a"), write("same.txt", b"b"))
            )
        with pytest.raises(PathRejected):  # a move's destination counts as a path of the batch
            await env.sandbox.write_files(
                env.handle,
                (
                    write("dest.txt", b"a"),
                    FileChangeV1(
                        op="move",
                        path="README.md",
                        expected_hash=sha(FILES["README.md"]),
                        move_to="dest.txt",
                    ),
                ),
            )
        with pytest.raises(SandboxLimit):
            await env.sandbox.write_files(
                env.handle, (write("huge.bin", b"\x00" * (MAX_FILE_BYTES + 1)),)
            )
        assert (await env.sandbox.status(env.handle)).revision == 0

    async def test_change_contracts_reject_malformed_changes(self, env: Env):
        for kwargs in (
            {"op": "write", "path": "a"},
            {"op": "delete", "path": "a"},
            {"op": "move", "path": "a", "expected_hash": sha(b"a")},
            {"op": "write", "path": "a", "content": b"", "move_to": "b"},
            {"op": "delete", "path": "a", "expected_hash": sha(b"a"), "content": b"x"},
        ):
            with pytest.raises(ValidationError):
                FileChangeV1(**kwargs)

    async def test_concurrent_writers_are_serialised_and_exactly_one_conflicting_writer_wins(
        self, env: Env
    ):
        results = await asyncio.gather(
            *(env.sandbox.write_files(env.handle, (write(f"c{i}.txt", b"x"),)) for i in range(20))
        )
        assert sorted(r.revision for r in results) == list(range(1, 21))
        racers = await asyncio.gather(
            *(
                env.sandbox.write_files(
                    env.handle,
                    (write("src/util.py", f"X = {i}\n".encode(), sha(FILES["src/util.py"])),),
                )
                for i in range(8)
            ),
            return_exceptions=True,
        )
        assert (
            sum(isinstance(r, WorkspaceConflict) for r in racers) == 7
            and sum(not isinstance(r, Exception) for r in racers) == 1
        )

    # --- exec ----------------------------------------------------------------------------------

    async def test_exec_streams_output_and_ends_with_exactly_one_exit_chunk(self, env: Env):
        chunks = await run(
            env.sandbox,
            env.handle,
            self.cmd(env.sandbox, stdout="hello\n", stderr="warn\n", exit_code=3),
        )
        assert [c.kind for c in chunks].count("exit") == 1 and chunks[-1].kind == "exit"
        assert "".join(c.text for c in chunks if c.kind == "stdout") == "hello\n"
        assert "".join(c.text for c in chunks if c.kind == "stderr") == "warn\n"
        assert chunks[-1].exit_code == 3 and not chunks[-1].timed_out and not chunks[-1].truncated

    async def test_exec_enforces_the_timeout(self, env: Env):
        chunks = await run(env.sandbox, env.handle, self.cmd(env.sandbox, delay_s=5), timeout_s=1)
        assert chunks[-1].kind == "exit" and chunks[-1].timed_out and chunks[-1].exit_code is None
        assert chunks[-1].duration_ms < 4000

    async def test_exec_caps_retained_output(self, env: Env):
        chunks = await run(
            env.sandbox,
            env.handle,
            self.cmd(env.sandbox, stdout_bytes=MAX_EXEC_OUTPUT_BYTES + 4096),
        )
        assert (
            sum(len(c.text.encode()) for c in chunks if c.kind == "stdout") <= MAX_EXEC_OUTPUT_BYTES
        )
        assert chunks[-1].truncated

    async def test_exec_rejects_bad_working_directories_and_oversized_timeouts(self, env: Env):
        for cwd in ("../x", "/", "missing-dir", ".git"):
            with pytest.raises((PathRejected, ExecRejected)):
                await run(env.sandbox, env.handle, self.cmd(env.sandbox), cwd=cwd)
        with pytest.raises(ExecRejected):
            await env.sandbox.acquire(env.spec(command_timeout_s=5))
            small = await env.sandbox.acquire(env.spec(session=uuid4(), command_timeout_s=5))
            await run(env.sandbox, small, self.cmd(env.sandbox), timeout_s=600)
        assert (await run(env.sandbox, env.handle, self.cmd(env.sandbox), cwd="src"))[
            -1
        ].exit_code == 0

    async def test_exec_requests_reject_dangerous_environments_and_arguments(self, env: Env):
        for env_vars in (
            {"LD_PRELOAD": "/x.so"},
            {"DYLD_INSERT_LIBRARIES": "x"},
            {"BASH_ENV": "/x"},
            {"bad key": "v"},
            {"A": "v"} | {f"K{i}": "v" for i in range(40)},
        ):
            with pytest.raises(ValidationError):
                ExecRequestV1(argv=("true",), env=env_vars)
        with pytest.raises(ValidationError):
            ExecRequestV1(argv=("echo", "a\x00b"))
        with pytest.raises(ValidationError):
            ExecRequestV1(argv=())
        assert ExecRequestV1(argv=("true",), env={"CI": "1", "LANG": "C"}).env == {
            "CI": "1",
            "LANG": "C",
        }

    # --- checkpoints, suspend ------------------------------------------------------------------

    async def test_restore_returns_to_a_checkpoint_content_exactly_and_moves_forward_in_revisions(
        self, env: Env
    ):
        await env.sandbox.write_files(env.handle, (write("kept.txt", b"keep"),))
        checkpoint = await env.sandbox.checkpoint(env.handle, "after kept")
        assert checkpoint.label == "after kept" and checkpoint.revision == 1
        await env.sandbox.write_files(
            env.handle,
            (
                write("later.txt", b"later"),
                write("src/util.py", b"X = 2\n", sha(FILES["src/util.py"])),
            ),
        )
        restored = await env.sandbox.restore(env.handle, checkpoint)
        assert (
            restored.tree_hash == checkpoint.tree_hash and restored.revision == 3
        )  # never goes backwards
        assert set(restored.changed_paths) == {"later.txt", "src/util.py"}
        with pytest.raises(WorkspaceFileNotFound):
            await env.sandbox.read_file(env.handle, "later.txt")
        assert (await env.sandbox.read_file(env.handle, "src/util.py")).content == "X = 1\n"
        again = await env.sandbox.restore(
            env.handle, checkpoint
        )  # restoring the same point is safe to repeat
        assert again.tree_hash == checkpoint.tree_hash and again.revision == 4

    async def test_a_checkpoint_belongs_to_its_sandbox_and_cannot_be_forged(self, env: Env):
        checkpoint = await env.sandbox.checkpoint(env.handle)
        other = await env.sandbox.acquire(env.spec(session=uuid4()))
        await env.sandbox.materialize(other, env.ref)
        with pytest.raises(CheckpointUnknown):
            await env.sandbox.restore(other, checkpoint)
        with pytest.raises(CheckpointUnknown):
            await env.sandbox.restore(
                env.handle, checkpoint.model_copy(update={"tree_hash": "0" * 64})
            )
        with pytest.raises(CheckpointUnknown):
            await env.sandbox.restore(env.handle, checkpoint.model_copy(update={"id": uuid4()}))

    async def test_suspend_keeps_the_workspace_and_the_next_call_resumes(self, env: Env):
        await env.sandbox.write_files(env.handle, (write("kept.txt", b"keep"),))
        before = await env.sandbox.status(env.handle)
        await env.sandbox.suspend(env.handle)
        assert env.sandbox.capabilities.supports_suspend in {True, False}
        after = await env.sandbox.status(env.handle)  # resumes transparently
        assert (after.revision, after.tree_hash) == (before.revision, before.tree_hash)
        assert (await env.sandbox.read_file(env.handle, "kept.txt")).content == "keep"
