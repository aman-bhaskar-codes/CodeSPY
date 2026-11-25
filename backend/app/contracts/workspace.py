"""Contracts of the execution sandbox port: workspaces, file changes, commands and checkpoints.

A sandbox is the only place uploaded source is edited or run. These contracts carry no host
paths, URLs or credentials: a workspace is a set of relative paths and bytes, and every file
has a content hash.
"""

import re
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from app.domain.scope import Contract

Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
RelPath = Annotated[str, Field(min_length=1, max_length=1024)]

MAX_ENV_VARS = 32
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
# Variables that change how a program is loaded or how a shell starts. Never settable by a caller.
_FORBIDDEN_ENV = frozenset(
    {"BASH_ENV", "ENV", "IFS", "PS4", "SHELLOPTS", "BASHOPTS", "PYTHONSTARTUP", "NODE_OPTIONS"}
)
_FORBIDDEN_ENV_PREFIXES = ("LD_", "DYLD_")


class SandboxSpecV1(Contract):
    """What a session asks for. The same spec for the same session attaches to the same sandbox."""

    org_id: UUID
    session_id: UUID
    image: Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:/@-]+$")]
    cpu: int = Field(default=2, ge=1, le=16)
    memory_mib: int = Field(default=4096, ge=512, le=32768)
    disk_mib: int = Field(default=10240, ge=1024, le=32768)
    pids: int = Field(default=256, ge=16, le=4096)
    egress: Literal["none", "registry_proxy"] = "none"
    command_timeout_s: int = Field(default=600, ge=1, le=600)
    idle_seconds: int = Field(default=900, ge=60, le=3600)
    lifetime_seconds: int = Field(default=14400, ge=300, le=28800)


class SandboxHandleV1(Contract):
    """Opaque to callers. Holding a handle is not authority: every call is checked."""

    id: UUID
    org_id: UUID
    session_id: UUID
    state: Literal["running", "suspended", "released"]


class SandboxCapabilitiesV1(Contract):
    supports_suspend: bool
    isolation: Literal["fake", "container", "gvisor", "microvm", "task"]
    network_egress: Literal["none", "proxy"]


class SnapshotRefV1(Contract):
    org_id: UUID
    repository_id: UUID
    snapshot_id: UUID


class WorkspaceRevisionV1(Contract):
    revision: int = Field(ge=0)
    tree_hash: Hash
    file_count: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    changed_paths: tuple[RelPath, ...] = Field(default=(), max_length=1000)


class FileEntryV1(Contract):
    path: RelPath
    size: int = Field(ge=0)
    file_hash: Hash


class FileListingV1(Contract):
    entries: tuple[FileEntryV1, ...] = Field(max_length=2000)
    truncated: bool = False


class FileSliceV1(Contract):
    path: RelPath
    file_hash: Hash
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=0)
    total_lines: int = Field(ge=0)
    content: str
    truncated: bool = False


class FileChangeV1(Contract):
    """One change in an atomic batch.

    A create names no expected hash; every other change names one.
    """

    op: Literal["write", "delete", "move"]
    path: RelPath
    expected_hash: Hash | None = None
    content: bytes | None = None
    move_to: RelPath | None = None

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if self.op == "write" and self.content is None:
            raise ValueError("a write carries content")
        if self.op == "delete" and (self.content is not None or self.expected_hash is None):
            raise ValueError("a delete names the expected hash and carries no content")
        if self.op == "move" and (
            self.move_to is None or self.expected_hash is None or self.content is not None
        ):
            raise ValueError("a move names its destination and the expected hash")
        if self.op != "move" and self.move_to is not None:
            raise ValueError("only a move has a destination")
        return self


class ExecRequestV1(Contract):
    argv: tuple[Annotated[str, Field(max_length=4096)], ...] = Field(min_length=1, max_length=64)
    cwd: RelPath = "."
    timeout_s: int = Field(default=600, ge=1, le=600)
    env: dict[str, Annotated[str, Field(max_length=4096)]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _env(self) -> Self:
        if len(self.env) > MAX_ENV_VARS:
            raise ValueError("too many environment variables")
        for key in self.env:
            if (
                not _ENV_KEY.fullmatch(key)
                or key in _FORBIDDEN_ENV
                or key.startswith(_FORBIDDEN_ENV_PREFIXES)
            ):
                raise ValueError("environment variable not allowed")
        if not self.argv[0]:
            raise ValueError("the program name is not empty")
        if any("\x00" in part for part in self.argv):
            raise ValueError("argument contains NUL")
        return self


class ExecChunkV1(Contract):
    """Output as it is produced, then exactly one ``exit`` chunk."""

    kind: Literal["stdout", "stderr", "exit"]
    text: str = ""
    exit_code: int | None = None
    timed_out: bool = False
    truncated: bool = False
    #: Set when the sandbox stopped the command for using more of a resource than it was given.
    limit_exceeded: Literal["disk"] | None = None
    duration_ms: int = Field(default=0, ge=0)


class CheckpointRefV1(Contract):
    id: UUID
    revision: int = Field(ge=0)
    tree_hash: Hash
    label: Annotated[str, Field(max_length=256)] = ""
