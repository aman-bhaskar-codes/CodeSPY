import hashlib

import pytest
from app.adapters.storage.local import LocalObjectStore


async def test_owned_orphan_is_described_from_bytes_without_trusting_metadata(tmp_path):
    store = LocalObjectStore(tmp_path / "objects")
    assert await store.describe_owned("stage/missing", 100) is None
    (store.root / "stage").mkdir()
    (store.root / "stage/orphan").write_bytes(b"actual bytes")
    found = await store.describe_owned("stage/orphan", 100)
    assert found.sha256 == hashlib.sha256(b"actual bytes").hexdigest()
    await store.delete_exact(found)
    assert await store.describe_owned("stage/orphan", 100) is None
    (store.root / "stage/oversize").write_bytes(b"1234")
    with pytest.raises(ValueError):
        await store.describe_owned("stage/oversize", 3)
    (store.root / "stage/link").symlink_to("/etc/hosts")
    with pytest.raises(OSError):
        await store.describe_owned("stage/link", 100)
