import pytest
from app.adapters.storage.local import LocalObjectStore
from app.domain.errors import DuplicateConflict


async def chunks(value):
    yield value


async def test_immutable_create_verify_and_delete(tmp_path):
    store = LocalObjectStore(tmp_path / "owned")
    a = await store.put_stage("stage/fixture/a", chunks(b"abc"), 10)
    b = await store.put_if_absent("source/fixture/abc/source.zip", a)
    assert b.sha256 == a.sha256 and await store.read_verified(b) == b"abc"
    assert await store.put_if_absent(b.key, a) == b
    wrong = await store.put_stage("stage/fixture/b", chunks(b"different"), 20)
    with pytest.raises(DuplicateConflict):
        await store.put_if_absent(b.key, wrong)
    with pytest.raises(ValueError):
        await store.put_stage("../outside", chunks(b"x"), 10)
    await store.delete_exact(b)
    assert not (tmp_path / "owned" / b.key).exists()


async def test_interrupted_stream_removes_only_its_stage(tmp_path):
    store = LocalObjectStore(tmp_path / "owned")

    async def broken():
        yield b"partial"
        raise RuntimeError("synthetic interruption")

    with pytest.raises(RuntimeError):
        await store.put_stage("stage/interrupted", broken(), 20)
    assert not (store.root / "stage/interrupted").exists()


async def test_byte_limit_and_symlink_escape(tmp_path):
    store = LocalObjectStore(tmp_path / "owned")
    with pytest.raises(ValueError):
        await store.put_stage("stage/too-big", chunks(b"abcd"), 3)
    assert not (store.root / "stage/too-big").exists()
    outside = tmp_path / "outside"
    outside.mkdir()
    (store.root / "stage" / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        await store.put_stage("stage/escape/file", chunks(b"x"), 10)
    assert not (outside / "file").exists()


async def test_exact_descriptor_and_concurrent_creation(tmp_path):
    import asyncio

    store = LocalObjectStore(tmp_path / "owned")
    stage = await store.put_stage("stage/fixture", chunks(b"abc"), 3)
    a, b = await asyncio.gather(
        store.put_if_absent("source/x", stage), store.put_if_absent("source/x", stage)
    )
    assert a == b
    with pytest.raises(DuplicateConflict):
        await store.delete_exact(a.model_copy(update={"sha256": "0" * 64}))
    assert await store.read_verified(a) == b"abc"
    with pytest.raises(ValueError):
        await store.delete_exact(a.model_copy(update={"version_id": "foreign-version"}))


async def test_repeated_cancellation_drains_thread_before_return(tmp_path):
    import asyncio
    import threading

    store = LocalObjectStore(tmp_path / "owned")
    ready = asyncio.Event()
    gate = threading.Event()
    completed = threading.Event()
    loop = asyncio.get_running_loop()

    def operation():
        loop.call_soon_threadsafe(ready.set)
        gate.wait(timeout=2)
        completed.set()

    task = asyncio.create_task(store._io(operation))
    try:
        async with asyncio.timeout(1):
            await ready.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        for _ in range(5):
            await asyncio.sleep(0)
        assert not task.done(), "descriptor ownership must survive repeated cancellation"
    finally:
        gate.set()
        await asyncio.gather(task, return_exceptions=True)
    assert completed.is_set()
