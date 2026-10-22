"""Offline dependency assessment uses explicit source and execution capabilities."""

from pathlib import Path
from typing import Protocol

from app.contracts.dependencies import DependencyArchiveV1, DependencyRequestV1, DependencyResultV1


class DependencyStager(Protocol):
    async def stage(self, request: DependencyRequestV1, root: Path) -> DependencyArchiveV1: ...


class DependencyAssessmentPort(Protocol):
    async def assess(self, request: DependencyRequestV1) -> DependencyResultV1: ...
