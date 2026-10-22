"""Durable scanner cleanup never confuses daemon failure with confirmed absence."""

import json

import pytest
from app.scanners.ownership import ScannerOwnership
from app.scanners.process import NativeProcessError

IMAGE = "sha256:" + "a" * 64


class MockController:
    """Control contract double; does not qualify Docker or OS isolation."""

    def __init__(self):
        self.present = {}
        self.fail = False
        self.removed = []

    async def inspect_owned(self, name):
        if self.fail:
            raise NativeProcessError("scanner_runtime_unavailable")
        return self.present.get(name)

    async def remove(self, identity):
        self.removed.append(identity)


async def test_crash_record_survives_daemon_failure_and_recovers_on_restart(tmp_path):
    controller = MockController()
    owner = ScannerOwnership.create(tmp_path / "owned", IMAGE)
    name = owner.reserve()
    assert (owner.root / (name + ".json")).exists()
    controller.fail = True
    with pytest.raises(NativeProcessError):
        await owner.remove(name, controller)
    assert (owner.root / (name + ".json")).exists()
    restored = ScannerOwnership.restore(owner.root, IMAGE)
    controller.fail = False
    controller.present[name] = {"id": "b" * 64, "image": IMAGE, "marker": owner.marker}
    await restored.cleanup_pending(controller)
    assert controller.removed == ["b" * 64]
    assert not (owner.root / (name + ".json")).exists()


async def test_confirmed_absence_removes_only_its_owned_pending_record(tmp_path):
    controller = MockController()
    owner = ScannerOwnership.create(tmp_path / "owned", IMAGE)
    first, second = owner.reserve(), owner.reserve()
    await owner.remove(first, controller)
    assert not (owner.root / (first + ".json")).exists()
    assert (owner.root / (second + ".json")).exists()
    assert controller.removed == []


@pytest.mark.parametrize("field", ["marker", "image"])
async def test_foreign_actual_container_is_never_removed(tmp_path, field):
    owner = ScannerOwnership.create(tmp_path / "owned", IMAGE)
    name = owner.reserve()
    controller = MockController()
    record = {"id": "b" * 64, "image": IMAGE, "marker": owner.marker}
    record[field] = "foreign"
    controller.present[name] = record
    with pytest.raises(ValueError):
        await owner.remove(name, controller)
    assert controller.removed == []
    assert (owner.root / (name + ".json")).exists()


async def test_tampered_pending_record_does_not_authorize_cleanup(tmp_path):
    owner = ScannerOwnership.create(tmp_path / "owned", IMAGE)
    name = owner.reserve()
    path = owner.root / (name + ".json")
    value = json.loads(path.read_text())
    value["name"] = "unrelated"
    path.write_text(json.dumps(value))
    controller = MockController()
    with pytest.raises(ValueError):
        await owner.cleanup_pending(controller)
    assert controller.removed == []


async def test_successful_recovery_removes_only_its_private_staging_directory(tmp_path):
    owner = ScannerOwnership.create(tmp_path / "owned", IMAGE)
    name = owner.reserve()
    staged = owner.root / (name + ".input")
    staged.mkdir()
    (staged / "archive.zip").write_bytes(b"synthetic")
    unrelated = owner.root / "other-input"
    unrelated.mkdir()
    await owner.remove(name, MockController())
    assert not staged.exists()
    assert unrelated.exists()


async def test_cancellation_during_failed_cleanup_remains_cancellation(tmp_path):
    import asyncio

    from app.scanners.ownership import cleanup_owned
    from app.scanners.process import NativeProcessError

    owner = ScannerOwnership.create(tmp_path / "owned-cancel", "sha256:" + "a" * 64)
    name = owner.reserve()
    entered = asyncio.Event()
    release = asyncio.Event()

    class Unavailable:
        async def inspect_owned(self, name):
            entered.set()
            await release.wait()
            raise NativeProcessError("scanner_runtime_unavailable")

        async def remove(self, identity):
            raise AssertionError("must not remove unknown container")

    task = asyncio.create_task(cleanup_owned(owner, Unavailable(), [name]))
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (owner.root / (name + ".json")).exists()


async def test_cancellation_survives_bounded_cleanup_timeout(tmp_path, monkeypatch):
    import asyncio

    from app.scanners.ownership import cleanup_owned

    owner = ScannerOwnership.create(tmp_path / "owned-timeout", "sha256:" + "a" * 64)
    name = owner.reserve()
    entered = asyncio.Event()
    original_timeout = asyncio.timeout
    monkeypatch.setattr(asyncio, "timeout", lambda _: original_timeout(0.02))

    class Unavailable:
        async def inspect_owned(self, name):
            entered.set()
            await asyncio.Event().wait()

        async def remove(self, identity):
            raise AssertionError("must not remove unknown container")

    task = asyncio.create_task(cleanup_owned(owner, Unavailable(), [name]))
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (owner.root / (name + ".json")).exists()
