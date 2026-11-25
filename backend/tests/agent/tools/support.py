"""A fake sandbox holding a small repository, plus fakes for the base snapshot and the indexes."""

from collections.abc import Mapping
from uuid import uuid4

from app.adapters.sandbox.fake import FakeSandbox, MappingSnapshots, ScriptedExec
from app.contracts.workspace import FileChangeV1, SandboxSpecV1, SnapshotRefV1
from app.services.agent.tools import read_tools
from app.services.agent.tools.base import ToolContext
from app.services.agent.tools.ports import SearchHit, SymbolHit

SNAPSHOT = uuid4()

# Secrets of every recognised shape. None of these strings may ever reach a model.
AWS = "AKIAIOSFODNN7EXAMPLE"
GITHUB = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
SLACK = "xoxb-1234567890-abcdefghijkl"
CANARY = "codespy_sec_canary_7f3a9c"
PASSWORD = "correct-horse-battery"  # noqa: S105 -- a fake secret the tests must never see leak
# Assembled at import so the source file holds no complete key block for a secret scanner to flag.
PRIVATE_KEY = (
    "-----BEGIN RSA "
    + "PRIVATE KEY-----\nMIIEowIBAAKCAQEAxxxxxxxxxxxxxxxx\nyyyyyyyyyyyyyyyyyyyyyyyy\n"
    "-----END RSA " + "PRIVATE KEY-----"
)
SECRETS = (
    AWS,
    GITHUB,
    SLACK,
    CANARY,
    PASSWORD,
    "MIIEowIBAAKCAQEAxxxxxxxxxxxxxxxx",
    "yyyyyyyyyyyyyyyyyyyyyyyy",
)

CONFIG = (
    "# configuration\n"
    f'AWS_KEY = "{AWS}"\n'
    f'TOKEN = "{GITHUB}"\n'
    f"slack = '{SLACK}'\n"
    f'password = "{PASSWORD}"\n'
    f"marker = {CANARY}\n"
    f"{PRIVATE_KEY}\n"
    "AFTER_THE_KEY = 1\n"
)

FILES: dict[str, bytes] = {
    "README.md": b"# demo\n",
    "src/app.py": b"def main():\n    return handle()\n\n\ndef handle():\n    return 1\n",
    "src/util.py": b"X = 1\n",
    "src/config.py": CONFIG.encode(),
    ".env": f"SECRET_KEY={AWS}\n".encode(),
    "certs/server.pem": PRIVATE_KEY.encode(),
    "data/blob.bin": b"\x00\x01\x02",
    "docs/guide.md": b"guide\n" * 3,
    "big.txt": "".join(f"line {i}\n" for i in range(1, 2001)).encode(),
}


class SpySandbox(FakeSandbox):
    """Records which files were read, so a test can prove a withheld file was never touched."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.reads: list[str] = []

    async def read_file(self, handle, path, start_line=1, end_line=None):
        self.reads.append(path)
        return await super().read_file(handle, path, start_line, end_line)

    async def read_bytes(self, handle, path):
        self.reads.append(path)
        return await super().read_bytes(handle, path)


class FakeBase:
    def __init__(self, files: Mapping[str, bytes]):
        import hashlib

        self.files = dict(files)
        self._hashes = {p: hashlib.sha256(d).hexdigest() for p, d in files.items()}

    async def hashes(self):
        return self._hashes

    async def read(self, path):
        return self.files.get(path)


class FakeSymbols:
    def __init__(self, hits):
        self.hits = hits
        self.calls = []

    async def find(self, name, kind, limit):
        self.calls.append((name, kind, limit))
        return [h for h in self.hits if h.name == name and (kind is None or h.kind == kind)][:limit]


class FakeSearch:
    def __init__(self, hits):
        self.hits = hits

    async def search(self, query, limit):
        return self.hits[:limit]


async def make_context(
    *, files=None, base=True, symbols=None, search=None
) -> tuple[ToolContext, SpySandbox]:
    files = FILES if files is None else files
    sandbox = SpySandbox(MappingSnapshots({SNAPSHOT: files}))
    org = uuid4()
    handle = await sandbox.acquire(
        SandboxSpecV1(org_id=org, session_id=uuid4(), image="test:latest")
    )
    await sandbox.materialize(
        handle, SnapshotRefV1(org_id=org, repository_id=uuid4(), snapshot_id=SNAPSHOT)
    )
    sandbox.reads.clear()
    ctx = ToolContext(
        sandbox=sandbox,
        handle=handle,
        base=FakeBase(files) if base else None,
        symbols=symbols,
        search=search,
    )
    return ctx, sandbox


def registry():
    return read_tools()


async def call(ctx, tool_name, /, **args):
    import json

    return await registry().execute(ctx, tool_name, json.dumps(args))


async def edit(ctx, path, content: bytes, expected: str | None):
    await ctx.sandbox.write_files(
        ctx.handle, (FileChangeV1(op="write", path=path, content=content, expected_hash=expected),)
    )


def grep_output(*hits: tuple[str, int, str]) -> ScriptedExec:
    """What `grep -rnIHZ` prints: `path\\0line:text`."""
    return ScriptedExec(stdout="".join(f"./{p}\x00{n}:{t}\n" for p, n, t in hits))


__all__ = ["ScriptedExec", "SearchHit", "SymbolHit"]
