"""Private source staging survives process interruption and preserves foreign files."""

import asyncio

import pytest
from app.indexing.staging import IndexInputStaging
from app.scanners.ownership import ScannerOwnership


@pytest.mark.asyncio
async def test_normal_and_cancelled_staging_removes_only_owned_files(tmp_path):
    staging = IndexInputStaging(tmp_path)
    keep = tmp_path / "owner-file"
    keep.write_bytes(b"preserve")
    async with staging.open("sha256:" + "a" * 64) as root:
        (root / "source").write_bytes(b"private")
        assert root.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["owner-file"]
    entered = asyncio.Event()

    async def work():
        async with staging.open("sha256:" + "a" * 64) as root:
            (root / "source").write_bytes(b"private")
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(work())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sorted(p.name for p in tmp_path.iterdir()) == ["owner-file"]


@pytest.mark.asyncio
async def test_exclusive_recovery_removes_registered_crash_input_only(tmp_path):
    root = tmp_path / ("index-stage-" + "a" * 32)
    owner = ScannerOwnership.create(root, "sha256:" + "b" * 64)
    name = owner.reserve()
    (root / (name + ".input")).mkdir()
    (root / (name + ".input") / "source").write_bytes(b"interrupted private bytes")
    await IndexInputStaging(tmp_path).recover_owned()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_foreign_stage_directory_is_never_deleted(tmp_path):
    root = tmp_path / ("index-stage-" + "a" * 32)
    root.mkdir()
    (root / "source").write_bytes(b"owner file")
    with pytest.raises((ValueError, FileNotFoundError)):
        await IndexInputStaging(tmp_path).recover_owned()
    assert (root / "source").read_bytes() == b"owner file"
