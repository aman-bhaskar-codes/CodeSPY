"""Maintainer-controlled scanner layout; never populated from uploaded content."""

from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from app.contracts.scanners import Digest, RuleId, ScannerProfileV1
from app.domain.scope import Contract


class ToolRuntime(Contract):
    tool: Literal["semgrep", "gitleaks"]
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    executable: Path
    artifact: Path
    rules: Path
    ignore_file: Path | None = None
    artifact_sha256: Digest
    rules_sha256: Digest
    rule_ids: tuple[RuleId, ...] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def trusted_layout(self) -> Self:
        paths: tuple[Path, ...] = (self.executable, self.artifact, self.rules)
        if self.ignore_file is not None:
            paths += (self.ignore_file,)
        if any(not p.is_absolute() or ".." in p.parts for p in paths):
            raise ValueError("absolute trusted runtime paths required")
        if len(set(self.rule_ids)) != len(self.rule_ids):
            raise ValueError("duplicate runtime rule identity")
        return self

    def profile(self) -> ScannerProfileV1:
        return ScannerProfileV1(
            tool=self.tool,
            artifact_sha256=self.artifact_sha256,
            rules_sha256=self.rules_sha256,
            rule_ids=self.rule_ids,
        )


class RuntimeManifest(Contract):
    schema_version: Literal[1] = 1
    tools: tuple[ToolRuntime, ...] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def unique(self) -> Self:
        if len({tool.tool for tool in self.tools}) != len(self.tools):
            raise ValueError("duplicate scanner runtime")
        return self

    def select(self, profile: ScannerProfileV1) -> ToolRuntime:
        for tool in self.tools:
            if tool.profile() == profile:
                return tool
        raise ValueError("unqualified scanner profile")


def load_manifest(path: Path) -> RuntimeManifest:
    """Validate a bounded runtime manifest and each pinned regular artifact."""
    import hashlib
    import os
    import stat

    from app.scanners.normalization import ScannerOutputError

    def digest(file: Path, maximum: int) -> str:
        descriptor = os.open(file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        value = hashlib.sha256()
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
                raise ScannerOutputError()
            total = 0
            while block := stream.read(65536):
                total += len(block)
                if total > maximum:
                    raise ScannerOutputError()
                value.update(block)
            if total != info.st_size:
                raise ScannerOutputError()
        return value.hexdigest()

    try:
        digest(path, 65536)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ScannerOutputError()
        manifest = RuntimeManifest.model_validate_json(raw)
        for tool in manifest.tools:
            digest(tool.executable, 268435456)
            if (
                digest(tool.artifact, 268435456) != tool.artifact_sha256
                or digest(tool.rules, 65536) != tool.rules_sha256
            ):
                raise ScannerOutputError()
            if tool.tool == "gitleaks" and (
                tool.ignore_file is None
                or digest(tool.ignore_file, 0) != hashlib.sha256(b"").hexdigest()
            ):
                raise ScannerOutputError()
        return manifest
    except (OSError, ValueError, TypeError):
        raise ScannerOutputError() from None
