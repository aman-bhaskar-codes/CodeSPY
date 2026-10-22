"""Metadata-only scanner capability supplied by trusted composition."""

from pathlib import Path
from typing import Protocol

from app.contracts.scanners import ScannerArchiveV1, ScannerRequestV1, ScannerResultV1


class ScannerPort(Protocol):
    async def scan(self, request: ScannerRequestV1) -> ScannerResultV1: ...


class ScannerStager(Protocol):
    async def stage(self, request: ScannerRequestV1, root: "Path") -> "ScannerArchiveV1": ...
