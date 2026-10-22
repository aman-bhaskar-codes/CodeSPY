"""Independent isolated range verification without invoking a native scanner."""

from pathlib import Path
from typing import Self

from pydantic import model_validator

from app.analyzers.positions import SourceIndex
from app.contracts.scanners import ScannerArchiveV1, ScannerResultV1
from app.domain.canonical_json import canonical_bytes, semantic_digest
from app.domain.scope import Contract
from app.scanners.source import read_scanner_sources


class ScannerAttestationV1(Contract):
    source: ScannerArchiveV1
    result: ScannerResultV1

    @model_validator(mode="after")
    def bound(self) -> Self:
        self.result.validate_for(self.source.request)
        if len(canonical_bytes(self.model_dump())) > 10485760:
            raise ValueError("attestation input exceeds budget")
        return self


def attest(value: ScannerAttestationV1, root: Path) -> str:
    sources = read_scanner_sources(value.source, root)
    for file_id, data in sources.items():
        observations = [o for o in value.result.observations if o.file_id == file_id]
        if observations:
            index = SourceIndex(data)
            for observation in observations:
                index.validate_range(observation.range)
    return semantic_digest(value.result.model_dump())
