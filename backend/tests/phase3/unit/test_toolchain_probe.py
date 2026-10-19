from pathlib import Path

import pytest
from scripts.qualify_structural_toolchain import read_image, validate_ownership


def test_probe_refuses_tags_and_symlink_image_files(tmp_path):
    image = tmp_path / "image.id"
    image.write_text("node:latest\n")
    with pytest.raises(ValueError, match="immutable"):
        read_image(image)
    image.write_text("sha256:" + "a" * 64)
    alias = tmp_path / "alias"
    alias.symlink_to(image)
    with pytest.raises(ValueError, match="regular"):
        read_image(alias)


def test_probe_refuses_cleanup_of_another_owner():
    with pytest.raises(ValueError, match="ownership"):
        validate_ownership({"codespy.phase3.probe": "other"}, "mine")
    validate_ownership({"codespy.phase3.probe": "mine"}, "mine")


def test_missing_probe_image_is_not_a_passing_capability(tmp_path):
    with pytest.raises(ValueError, match="regular"):
        read_image(Path(tmp_path / "absent"))


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
async def test_control_output_is_bounded_while_draining(stream):
    import sys

    from scripts.qualify_structural_toolchain import ProbeOutputLimit, bounded_command

    code = f"import sys; sys.{stream}.buffer.write(b'x'*131072); sys.{stream}.flush()"
    with pytest.raises(ProbeOutputLimit):
        await bounded_command([sys.executable, "-c", code], timeout=5)


async def test_cancelled_control_process_is_reaped(tmp_path):
    import asyncio
    import os
    import sys

    from scripts.qualify_structural_toolchain import bounded_command

    pid_file = tmp_path / "owned.pid"
    code = (
        "import os,sys,time; from pathlib import Path; "
        "Path(sys.argv[1]).write_text(str(os.getpid())); "
        "print('ready',flush=True); time.sleep(30)"
    )
    started = asyncio.Event()
    task = asyncio.create_task(
        bounded_command([sys.executable, "-c", code, str(pid_file)], timeout=5, started=started)
    )
    try:
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_large_flood_does_not_deadlock_termination(stream):
    import subprocess
    import sys

    controller = f"""
import asyncio, sys
from scripts.qualify_structural_toolchain import bounded_command, ProbeOutputLimit
async def check():
    child = "import sys; sys.{stream}.buffer.write(b'x'*10000000); sys.{stream}.flush()"
    try:
        await bounded_command([sys.executable, '-c', child], timeout=1)
    except ProbeOutputLimit:
        print('bounded', flush=True)
    else:
        raise AssertionError('oversized stream succeeded')
asyncio.run(check())
"""
    try:
        result = subprocess.run(  # noqa: S603 - fixed trusted flood fixture, no target input
            [sys.executable, "-c", controller],
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("pipe cleanup deadlocked after the output bound")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "bounded"
