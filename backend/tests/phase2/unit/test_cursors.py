from uuid import uuid4

import pytest
from app.security.cursors import CursorCodec


def test_cursor_binds_scope_filter_version_and_rejects_tampering():
    codec = CursorCodec(b"fixture-only-cursor-secret-32bytes")
    scope = {"org": str(uuid4()), "snapshot": str(uuid4()), "version": 1}
    cursor = codec.encode(scope, "src/a.py")
    assert codec.decode(cursor, scope) == "src/a.py"
    with pytest.raises(ValueError):
        codec.decode(cursor, {**scope, "snapshot": str(uuid4())})
    with pytest.raises(ValueError):
        codec.decode(cursor[:-1] + ("A" if cursor[-1] != "A" else "B"), scope)
