"""Trusted offline lock assessment inside the existing source sandbox."""

import hashlib
import io
import zipfile
from pathlib import Path
from typing import Literal, Self

from pydantic import model_validator

from app.analyzers.source import SourceIntegrityError, authenticate_archive
from app.contracts.dependencies import (
    DependencyArchiveV1,
    DependencyFileOutcomeV1,
    DependencyPackageV1,
    DependencyResultV1,
    PackageQuery,
    UnresolvedDependencyV1,
)
from app.domain.canonical_json import canonical_bytes
from app.domain.scope import Contract
from app.scanners.advisories import AdvisoryBundle
from app.scanners.dependencies import LockfileError, parse_package_lock, parse_uv_lock


class DependencyInputV1(Contract):
    source: DependencyArchiveV1
    bundle: AdvisoryBundle

    @model_validator(mode="after")
    def bound(self) -> Self:
        if self.source.request.bundle_digest != self.bundle.digest():
            raise ValueError("dependency bundle differs from request")
        if len(canonical_bytes(self.model_dump())) > 16777216:
            raise ValueError("dependency input exceeds budget")
        return self


def assess_dependencies(value: DependencyInputV1, root: Path) -> DependencyResultV1:
    source = value.source
    raw = authenticate_archive(source, root)
    stored = {file.file_id: file for file in source.files}
    outcomes: list[DependencyFileOutcomeV1] = []
    packages: list[DependencyPackageV1] = []
    unresolved: list[UnresolvedDependencyV1] = []
    assessments = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        paths = {
            (
                info.filename.split("/", 1)[1] if source.policy.strip_single_root else info.filename
            ): info
            for info in archive.infolist()
            if not info.is_dir()
        }
        for selected in source.request.files:
            file = stored[selected.file_id]
            if not file.eligible:
                outcomes.append(DependencyFileOutcomeV1(file_id=file.file_id, status="excluded"))
                continue
            with archive.open(paths[file.path]) as stream:
                data = stream.read(selected.size_bytes + 1)
            if (
                len(data) != selected.size_bytes
                or hashlib.sha256(data).hexdigest() != selected.sha256
            ):
                raise SourceIntegrityError("source_integrity_error")
            parser = parse_uv_lock if selected.format == "uv-v1-r3" else parse_package_lock
            try:
                inventory = parser(data)
            except LockfileError:
                outcomes.append(
                    DependencyFileOutcomeV1(file_id=file.file_id, status="unsupported_or_invalid")
                )
                continue
            if (
                len(packages)
                + len(unresolved)
                + len(inventory.packages)
                + len(inventory.unresolved)
                > 5000
            ):
                raise ValueError("dependency instance budget exceeded")
            outcomes.append(DependencyFileOutcomeV1(file_id=file.file_id, status="parsed"))
            for package in inventory.packages:
                query = PackageQuery(
                    ecosystem=package.ecosystem, name=package.name, version=package.version
                )
                if query.key() not in assessments:
                    assessments[query.key()] = value.bundle.evaluate(
                        query, now=source.request.evaluated_at
                    )
                packages.append(
                    DependencyPackageV1(
                        file_id=file.file_id,
                        location=package.location,
                        query=query,
                        marker_count=len(package.markers),
                        assessment=assessments[query.key()],
                    )
                )
            unresolved.extend(
                UnresolvedDependencyV1(file_id=file.file_id, location=location)
                for location in inventory.unresolved
            )
    complete = (
        bool(outcomes)
        and all(o.status == "parsed" for o in outcomes)
        and not unresolved
        and all(p.assessment.status in {"matched", "empty"} for p in packages)
    )
    coverage: Literal["complete", "partial", "unknown"] = (
        "complete"
        if complete
        else "partial"
        if any(o.status == "parsed" for o in outcomes)
        else "unknown"
    )
    result = DependencyResultV1(
        request_digest=source.request.semantic_hash(),
        coverage=coverage,
        files=tuple(outcomes),
        packages=tuple(packages),
        unresolved=tuple(unresolved),
    )
    result.validate_for(source.request)
    return result
