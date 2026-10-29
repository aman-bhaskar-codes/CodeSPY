"""Deterministic metadata selection from an explicitly registered scanner policy."""

from typing import Literal

from pydantic import Field

from app.contracts.dependencies import DependencyFileV1
from app.contracts.scan_jobs import DependencySelectionV1, NativeSelectionV1, ScanConfigV1
from app.contracts.scanners import ScannerFileV1
from app.contracts.structure import SourceFileV1
from app.domain.scope import Contract


class ScanConfigurationV1(Contract):
    configured: bool
    config: ScanConfigV1 | None = None
    reason: Literal["configured", "scanner_unavailable", "policy_unavailable", "no_eligible_files"]
    selected_source_files: int = Field(default=0, ge=0)
    omitted_source_files: int = Field(default=0, ge=0)
    selected_lockfiles: int = Field(default=0, ge=0)
    omitted_lockfiles: int = Field(default=0, ge=0)
    live_access_verified: Literal[False] = False


def choose_scan_configuration(
    template: ScanConfigV1, files: tuple[SourceFileV1, ...]
) -> ScanConfigurationV1:
    native: list[ScannerFileV1] = []
    dependency: list[DependencyFileV1] = []
    source_bytes = dependency_bytes = omitted_source = omitted_lockfiles = 0
    formats: dict[str, Literal["package-lock-v3", "uv-v1-r3"]] = {
        "package-lock.json": "package-lock-v3",
        "uv.lock": "uv-v1-r3",
    }
    for source in sorted(files, key=lambda file: (file.path, str(file.file_id))):
        if not source.eligible:
            continue
        if template.native_selections:
            if (
                len(native) < 128
                and source.size_bytes <= 2097152
                and source_bytes + source.size_bytes <= 16777216
            ):
                native.append(
                    ScannerFileV1(
                        file_id=source.file_id, sha256=source.sha256, size_bytes=source.size_bytes
                    )
                )
                source_bytes += source.size_bytes
            else:
                omitted_source += 1
        filename = source.path.rsplit("/", 1)[-1]
        if template.dependency_selection is not None and filename in formats:
            if (
                len(dependency) < 32
                and source.size_bytes <= 262144
                and dependency_bytes + source.size_bytes <= 2097152
            ):
                dependency.append(
                    DependencyFileV1(
                        file_id=source.file_id,
                        sha256=source.sha256,
                        size_bytes=source.size_bytes,
                        format=formats[filename],
                    )
                )
                dependency_bytes += source.size_bytes
            else:
                omitted_lockfiles += 1
    if not native and not dependency:
        return ScanConfigurationV1(
            configured=False,
            reason="no_eligible_files",
            omitted_source_files=omitted_source,
            omitted_lockfiles=omitted_lockfiles,
        )
    config = ScanConfigV1(
        runtime_digest=template.runtime_digest,
        catalog_digest=template.catalog_digest,
        native_selections=tuple(
            NativeSelectionV1(profile=selection.profile, files=tuple(native))
            for selection in template.native_selections
        )
        if native
        else (),
        dependency_selection=DependencySelectionV1(
            bundle_digest=template.dependency_selection.bundle_digest, files=tuple(dependency)
        )
        if dependency and template.dependency_selection is not None
        else None,
    )
    return ScanConfigurationV1(
        configured=True,
        reason="configured",
        config=config,
        selected_source_files=len(native),
        omitted_source_files=omitted_source,
        selected_lockfiles=len(dependency),
        omitted_lockfiles=omitted_lockfiles,
    )
