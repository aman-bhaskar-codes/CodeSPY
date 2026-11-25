# ruff: noqa: E501, S603, S607
"""What the container boundary guarantees: no network, no escape, bounded resources, nothing left behind.

Opt in with CODESPY_SANDBOX_IMAGE. Each test runs real hostile commands inside real containers.
"""

import subprocess
from uuid import uuid4

import pytest
from app.contracts.workspace import (
    ExecRequestV1,
    FileChangeV1,
    SandboxSpecV1,
    SnapshotRefV1,
)
from app.ports.sandbox import NotATextFile, PathRejected

from tests.agent.sandbox.docker_support import IMAGE, Harness, requires_docker

pytestmark = [pytest.mark.sandbox, requires_docker]

SNAPSHOT = uuid4()
FILES = {"src/app.py": b"print('hi')\n", "README.md": b"# demo\n"}


class Box:
    def __init__(self, harness: Harness):
        self.harness = harness
        self.sandbox = harness.sandbox
        self.org, self.repo, self.session = uuid4(), uuid4(), uuid4()
        self.handle = None

    async def start(self, **spec):
        self.handle = await self.sandbox.acquire(
            SandboxSpecV1(org_id=self.org, session_id=self.session, image=IMAGE, **spec)
        )
        await self.sandbox.materialize(
            self.handle,
            SnapshotRefV1(org_id=self.org, repository_id=self.repo, snapshot_id=SNAPSHOT),
        )
        return self

    async def run(self, *argv, timeout_s=60, **kw):
        chunks = [
            c
            async for c in self.sandbox.exec(
                self.handle, ExecRequestV1(argv=argv, timeout_s=timeout_s, **kw)
            )
        ]
        out = "".join(c.text for c in chunks if c.kind == "stdout")
        err = "".join(c.text for c in chunks if c.kind == "stderr")
        return out, err, chunks[-1]

    def sh(self, script, **kw):
        return self.run("sh", "-c", script, **kw)

    def py(self, code, **kw):
        return self.run("python3", "-c", code, **kw)


@pytest.fixture
async def make():
    harnesses = []

    async def factory(*, quota=None):
        harness = Harness({SNAPSHOT: FILES}, disk_quota_bytes=quota)
        harnesses.append(harness)
        return Box(harness)

    yield factory
    for harness in harnesses:
        await harness.close()


def containers(harness: Harness) -> list[str]:
    result = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label=codespy.sandbox.marker={harness.marker}"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return result.stdout.split()


async def test_commands_see_the_workspace_and_their_changes_are_recorded(make):
    box = await (await make()).start()
    out, _, exit_ = await box.sh(
        "cat src/app.py && echo made > out.txt && mkdir -p build && echo x > build/a.o"
    )
    assert out == "print('hi')\n" and exit_.exit_code == 0
    status = await box.sandbox.status(box.handle)
    assert status.revision == 1  # one command, one revision, however many files it touched
    assert (await box.sandbox.read_file(box.handle, "out.txt")).content == "made\n"
    assert (await box.sandbox.read_file(box.handle, "build/a.o")).content == "x\n"
    _, _, again = await box.sh("cat out.txt")
    assert (
        await box.sandbox.status(box.handle)
    ).revision == 1 and again.exit_code == 0  # read-only: no revision


async def test_there_is_no_network_at_all(make):
    box = await (await make()).start()
    out, _, exit_ = await box.py(
        "import socket\n"
        "for host, port in (('1.1.1.1', 53), ('169.254.169.254', 80), ('8.8.8.8', 443)):\n"
        "    try:\n"
        "        socket.create_connection((host, port), timeout=2); print('REACHED', host)\n"
        "    except OSError as e:\n"
        "        print('blocked', host)\n"
        "try:\n"
        "    socket.getaddrinfo('example.com', 80); print('RESOLVED')\n"
        "except OSError:\n"
        "    print('no dns')\n"
        "print(sorted(l.split(':')[0].strip() for l in open('/proc/net/dev').read().splitlines()[2:]))\n"
    )
    assert "REACHED" not in out and "RESOLVED" not in out and "no dns" in out
    assert out.strip().splitlines()[-1].count("'lo'") == 1
    routes, _, _ = await box.sh("cat /proc/net/route")
    assert len(routes.splitlines()) == 1  # a header and no route: nowhere to send a packet
    assert exit_.exit_code == 0 and (await box.sh("echo ok"))[0] == "ok\n"


async def test_the_process_is_unprivileged_and_the_root_filesystem_is_read_only(make):
    box = await (await make()).start()
    out, _, _ = await box.sh(
        "id -u; grep -E '^(CapEff|NoNewPrivs):' /proc/self/status; "
        "touch /etc/planted 2>&1 | head -1; touch /usr/planted 2>&1 | head -1; echo $HOME"
    )
    lines = out.splitlines()
    assert lines[0] != "0"  # not root
    assert "CapEff:\t0000000000000000" in lines and "NoNewPrivs:\t1" in lines
    assert sum("Read-only file system" in line for line in lines) == 2
    assert lines[-1] == "/home/sandbox"


async def test_a_fork_bomb_is_contained_reported_and_leaves_nothing_behind(make):
    box = await (await make()).start(pids=128)
    out, _, exit_ = await box.py(
        "import os, time\n"
        "n = 0\n"
        "try:\n"
        "    while n < 5000:\n"
        "        if os.fork() == 0:\n"
        "            time.sleep(60); os._exit(0)\n"
        "        n += 1\n"
        "except OSError:\n"
        "    pass\n"
        "print('forked', n)\n",
        timeout_s=40,
    )
    forked = int(out.split()[-1])
    assert 10 < forked < 200 and not exit_.timed_out  # stopped by the process limit, not by luck
    assert containers(box.harness) == []  # every child died with its container
    assert (await box.sh("echo alive"))[0] == "alive\n"  # and the next command is unaffected


async def test_an_overlong_command_is_stopped_and_its_container_removed(make):
    box = await (await make()).start()
    _, _, exit_ = await box.sh("sleep 660", timeout_s=2)
    assert exit_.timed_out and exit_.exit_code is None and exit_.duration_ms < 10_000
    assert containers(box.harness) == []
    with pytest.raises(ValueError, match="timeout_s"):
        ExecRequestV1(
            argv=("sleep", "660"), timeout_s=660
        )  # the contract itself caps a command at 10 minutes


async def test_filling_the_disk_is_stopped_and_reported_and_files_can_then_be_deleted(make):
    quota = 24 * 1024 * 1024
    box = await (await make(quota=quota)).start()
    _, _, exit_ = await box.sh(
        "i=0; while true; do dd if=/dev/zero of=fill$i bs=1M count=1 2>/dev/null; i=$((i+1)); done",
        timeout_s=60,
    )
    assert exit_.limit_exceeded == "disk" and exit_.exit_code is None and not exit_.timed_out
    assert containers(box.harness) == []
    used = (await box.sandbox.status(box.handle)).total_bytes
    assert quota < used < 2 * 1024**3  # stopped within a poll or two of crossing the limit
    listing = await box.sandbox.list_files(box.handle)
    victims = [e for e in listing.entries if e.path.startswith("fill")]
    batch = tuple(
        FileChangeV1(op="delete", path=e.path, expected_hash=e.file_hash) for e in victims[:200]
    )
    await box.sandbox.write_files(
        box.handle, batch
    )  # deletes are always allowed, so the agent can recover
    assert (await box.sandbox.status(box.handle)).total_bytes < used


async def test_a_single_huge_file_is_cut_off_by_the_file_size_limit(make):
    quota = 16 * 1024 * 1024
    box = await (await make(quota=quota)).start()
    _, _, exit_ = await box.sh("dd if=/dev/zero of=huge bs=1M count=200 2>/dev/null", timeout_s=60)
    assert exit_.exit_code != 0  # killed by SIGXFSZ
    assert (await box.sandbox.status(box.handle)).total_bytes <= quota + len(
        b"print('hi')\n"
    ) + len(b"# demo\n")


async def test_a_memory_hog_is_killed_without_harming_the_sandbox(make):
    box = await (await make()).start(memory_mib=512)
    _, _, exit_ = await box.py("x = bytearray(2 * 1024**3); print('allocated')", timeout_s=40)
    assert exit_.exit_code not in (0, None) and not exit_.timed_out
    assert (await box.sh("echo alive"))[0] == "alive\n"


async def test_links_a_command_plants_are_never_followed_by_the_host(make):
    box = await (await make()).start()
    await box.sh("ln -s /etc/hosts hosts-link; ln -s / rootlink; ln -s /etc etc-link; mkfifo pipe")
    listing = await box.sandbox.list_files(box.handle)
    assert sorted(e.path for e in listing.entries) == sorted(FILES)  # links and FIFOs are not files
    for path in ("hosts-link", "etc-link/hosts", "rootlink/etc/hosts", "pipe"):
        with pytest.raises((NotATextFile, PathRejected, Exception)) as raised:
            await box.sandbox.read_file(box.handle, path)
        assert type(raised.value).__name__ in {
            "NotATextFile",
            "PathRejected",
            "WorkspaceFileNotFound",
        }
    with pytest.raises(PathRejected):
        await box.sandbox.write_files(
            box.handle, (FileChangeV1(op="write", path="rootlink/tmp/planted", content=b"x"),)
        )
    out, _, _ = await box.sh("ls /tmp/planted 2>&1; echo done")
    assert "No such file" in out


async def test_a_restarted_process_reattaches_to_the_same_workspace_and_checkpoints(make):
    first = await (await make()).start()
    await first.sandbox.write_files(
        first.handle, (FileChangeV1(op="write", path="kept.txt", content=b"keep"),)
    )
    cp = await first.sandbox.checkpoint(first.handle, "kept")
    await first.sandbox.write_files(
        first.handle, (FileChangeV1(op="write", path="later.txt", content=b"later"),)
    )
    from app.adapters.sandbox.docker import DockerSandbox
    from app.adapters.sandbox.fake import MappingSnapshots

    second = DockerSandbox(
        first.harness.root,
        MappingSnapshots({SNAPSHOT: FILES}),
        image=IMAGE,
        marker=first.harness.marker,
    )
    handle = await second.acquire(
        SandboxSpecV1(org_id=first.org, session_id=first.session, image=IMAGE)
    )
    assert handle.id == first.handle.id  # the sandbox, not the process, owns the workspace
    status = await second.status(handle)
    assert status.revision == 2 and (await second.read_file(handle, "later.txt")).content == "later"
    restored = await second.restore(handle, cp)
    assert restored.tree_hash == cp.tree_hash and restored.revision == 3


async def test_a_released_sandbox_leaves_no_files_or_containers_behind(make):
    box = await (await make()).start()
    await box.sh("echo data > scratch.txt")
    directory = box.harness.root / str(box.org) / str(box.session)
    assert directory.exists()
    await box.sandbox.release(box.handle)
    assert not directory.exists() and containers(box.harness) == []


async def test_an_unknown_image_or_a_network_request_is_refused_at_acquire(make):
    box = await make()
    from app.ports.sandbox import SandboxStateError

    with pytest.raises(SandboxStateError):
        await box.sandbox.acquire(
            SandboxSpecV1(org_id=box.org, session_id=box.session, image="alpine:latest")
        )
    with pytest.raises(SandboxStateError):
        await box.sandbox.acquire(
            SandboxSpecV1(
                org_id=box.org, session_id=box.session, image=IMAGE, egress="registry_proxy"
            )
        )
