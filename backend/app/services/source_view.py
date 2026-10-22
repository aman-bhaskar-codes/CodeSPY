"""Verified SourceViewService: authorized, byte-exact, privacy-transformed source views."""

import hashlib
import io
import zipfile
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.source_view import (
    REMAINING_PRIVACY_RISK_LIMITATIONS,
    ByteRangeV1,
    IntervalMappingV1,
    PrivacyPolicyV1,
    SourceRepresentation,
    SourceViewRequestV1,
    SourceViewResultV1,
)
from app.domain.errors import ScopeViolation
from app.models.derived_artifacts import derived_artifacts
from app.models.phase2_tables import PHASE2_TABLES
from app.models.tables import memberships, source_files
from app.ports.object_store import ObjectStore
from app.services.analysis_sources import locate_source
from app.services.privacy_transform import (
    is_sensitive_path,
    slice_mappings_for_safe_range,
    transform_model_safe,
)


@dataclass(frozen=True)
class SourceFileRow:
    id: UUID
    path: str
    sha256: str
    size_bytes: int


class SourceViewService:
    """Provides authorized, verified source views with byte-exact privacy transforms."""

    def __init__(self, session: AsyncSession, store: ObjectStore) -> None:
        self.s = session
        self.store = store

    async def get(
        self,
        request: SourceViewRequestV1,
        policy: PrivacyPolicyV1,
    ) -> SourceViewResultV1:
        """Fetch verified source range under requested representation and policy."""
        # 1. Authority boundary check
        await self._verify_scope_and_representation(request, policy)

        # 2. Source file metadata lookup
        file_row = await self._get_source_file(request.org_id, request.snapshot_id, request.file_id)

        # 3. Sensitive path check for model-safe views
        if request.representation == SourceRepresentation.MODEL_SAFE and is_sensitive_path(
            file_row.path, policy.sensitive_path_patterns
        ):
            empty_sha = hashlib.sha256(b"").hexdigest()
            return SourceViewResultV1(
                file_id=request.file_id,
                file_path=file_row.path,
                file_sha256=file_row.sha256,
                representation=SourceRepresentation.MODEL_SAFE,
                requested_range=request.byte_range or ByteRangeV1(start_byte=0, end_byte=0),
                actual_range=ByteRangeV1(start_byte=0, end_byte=0),
                content_bytes=b"",
                content_text="",
                content_sha256=empty_sha,
                total_file_size=file_row.size_bytes,
                policy_version=policy.policy_version,
                redaction_map_digest=empty_sha,
                interval_mappings=(),
                remaining_privacy_risks=REMAINING_PRIVACY_RISK_LIMITATIONS,
                is_omitted=True,
                omission_reason="sensitive_path_exclusion",
            )

        # 4. Authenticated source bytes extraction
        raw_bytes = await self._extract_raw_source_bytes(
            request.org_id,
            request.repository_id,
            request.snapshot_id,
            file_row.path,
            file_row.sha256,
        )

        # 5. Handle representation
        if request.representation == SourceRepresentation.ORIGINAL:
            return self._build_original_view(raw_bytes, file_row, request.byte_range, policy)

        return await self._build_model_safe_view(raw_bytes, file_row, request, policy)

    async def _verify_scope_and_representation(
        self,
        request: SourceViewRequestV1,
        policy: PrivacyPolicyV1,
    ) -> None:
        """Enforce organization membership, repository grant, and representation rights."""
        # Check membership
        role = await self.s.scalar(
            select(memberships.c.role).where(
                memberships.c.org_id == request.org_id,
                memberships.c.principal_id == request.principal_id,
            )
        )
        if role is None:
            raise ScopeViolation("Principal is not a member of the organization")

        # Check repository grant
        grants = PHASE2_TABLES["repository_grants"]
        grant = (
            (
                await self.s.execute(
                    select(grants).where(
                        grants.c.org_id == request.org_id,
                        grants.c.repository_id == request.repository_id,
                        grants.c.principal_id == request.principal_id,
                        grants.c.revoked_at.is_(None),
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if grant is None:
            raise ScopeViolation("Repository access grant is missing or revoked")

        # Original representation authorization check
        if request.representation == SourceRepresentation.ORIGINAL:
            if not policy.allow_original_view:
                raise ScopeViolation("Policy denies original unredacted source representation")
            if role == "viewer":
                raise ScopeViolation(
                    "Viewer role is not authorized to inspect original unredacted code"
                )

    async def _get_source_file(
        self, org_id: UUID, snapshot_id: UUID, file_id: UUID
    ) -> SourceFileRow:
        row = (
            (
                await self.s.execute(
                    select(source_files).where(
                        source_files.c.org_id == org_id,
                        source_files.c.snapshot_id == snapshot_id,
                        source_files.c.id == file_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ScopeViolation(f"Source file {file_id} not found in snapshot")
        return SourceFileRow(
            id=UUID(str(row["id"])),
            path=str(row["path"]),
            sha256=str(row["sha256"]),
            size_bytes=int(row["size_bytes"]),
        )

    async def _extract_raw_source_bytes(
        self,
        org_id: UUID,
        repository_id: UUID,
        snapshot_id: UUID,
        file_path: str,
        expected_sha: str,
    ) -> bytes:
        located = await locate_source(self.s, org_id, repository_id, snapshot_id)

        raw_archive = bytearray()
        async for chunk in self.store.open_verified(located.archive):
            raw_archive.extend(chunk)
            if len(raw_archive) > 104_857_600:  # 100 MiB limit
                raise ScopeViolation("Source archive exceeds size limit")

        try:
            with zipfile.ZipFile(io.BytesIO(raw_archive)) as zf:
                content = zf.read(file_path)
        except (KeyError, zipfile.BadZipFile) as exc:
            raise ScopeViolation(f"File {file_path} cannot be read from archive") from exc

        actual_sha = hashlib.sha256(content).hexdigest()
        if actual_sha != expected_sha:
            raise ScopeViolation("Source content hash mismatch with admitted inventory")

        return content

    def _build_original_view(
        self,
        raw_bytes: bytes,
        file_row: SourceFileRow,
        requested_range: ByteRangeV1 | None,
        policy: PrivacyPolicyV1,
    ) -> SourceViewResultV1:
        total_len = len(raw_bytes)
        req = requested_range or ByteRangeV1(
            start_byte=0, end_byte=min(total_len, policy.max_view_bytes)
        )

        start_b = max(0, min(req.start_byte, total_len))
        end_b = max(start_b, min(req.end_byte, total_len))
        if end_b - start_b > policy.max_view_bytes:
            end_b = start_b + policy.max_view_bytes

        actual_range = ByteRangeV1(start_byte=start_b, end_byte=end_b)
        sliced_bytes = raw_bytes[start_b:end_b]
        sliced_sha = hashlib.sha256(sliced_bytes).hexdigest()

        try:
            text = sliced_bytes.decode("utf-8")
        except UnicodeDecodeError:
            text = sliced_bytes.decode("utf-8", errors="replace")

        identity_mapping = (
            IntervalMappingV1(
                safe_start_byte=0,
                safe_end_byte=len(sliced_bytes),
                original_start_byte=start_b,
                original_end_byte=end_b,
                is_redacted=False,
            ),
        )

        return SourceViewResultV1(
            file_id=file_row.id,
            file_path=file_row.path,
            file_sha256=file_row.sha256,
            representation=SourceRepresentation.ORIGINAL,
            requested_range=req,
            actual_range=actual_range,
            content_bytes=sliced_bytes,
            content_text=text,
            content_sha256=sliced_sha,
            total_file_size=total_len,
            policy_version=policy.policy_version,
            redaction_map_digest=sliced_sha,
            interval_mappings=identity_mapping,
            remaining_privacy_risks=(),
            is_omitted=False,
            omission_reason=None,
        )

    async def _build_model_safe_view(
        self,
        raw_bytes: bytes,
        file_row: SourceFileRow,
        request: SourceViewRequestV1,
        policy: PrivacyPolicyV1,
    ) -> SourceViewResultV1:
        # Check derived artifact registry for cached safe representation
        cached_row = (
            (
                await self.s.execute(
                    select(derived_artifacts).where(
                        derived_artifacts.c.org_id == request.org_id,
                        derived_artifacts.c.snapshot_id == request.snapshot_id,
                        derived_artifacts.c.file_id == request.file_id,
                        derived_artifacts.c.representation == "model_safe",
                        derived_artifacts.c.policy_version == policy.policy_version,
                        derived_artifacts.c.transformation_status == "ready",
                    )
                )
            )
            .mappings()
            .one_or_none()
        )

        transform_output = transform_model_safe(raw_bytes, policy)

        if cached_row is None:
            # Register in derived_artifacts
            map_data = [m.model_dump() for m in transform_output.mappings]
            await self.s.execute(
                derived_artifacts.insert().values(
                    org_id=request.org_id,
                    id=uuid4(),
                    repository_id=request.repository_id,
                    snapshot_id=request.snapshot_id,
                    file_id=request.file_id,
                    representation="model_safe",
                    policy_version=policy.policy_version,
                    source_sha256=file_row.sha256,
                    derived_sha256=hashlib.sha256(transform_output.safe_bytes).hexdigest(),
                    size_bytes=len(transform_output.safe_bytes),
                    redaction_digest=transform_output.redaction_map_digest,
                    transformation_status="ready",
                    interval_map_json=map_data,
                    storage_key=None,
                )
            )

        safe_total = len(transform_output.safe_bytes)
        req = request.byte_range or ByteRangeV1(
            start_byte=0, end_byte=min(safe_total, policy.max_view_bytes)
        )

        start_b = max(0, min(req.start_byte, safe_total))
        end_b = max(start_b, min(req.end_byte, safe_total))
        if end_b - start_b > policy.max_view_bytes:
            end_b = start_b + policy.max_view_bytes

        actual_range = ByteRangeV1(start_byte=start_b, end_byte=end_b)
        sliced_bytes = transform_output.safe_bytes[start_b:end_b]
        sliced_sha = hashlib.sha256(sliced_bytes).hexdigest()
        sliced_mappings = slice_mappings_for_safe_range(transform_output.mappings, start_b, end_b)

        try:
            text = sliced_bytes.decode("utf-8")
        except UnicodeDecodeError:
            text = sliced_bytes.decode("utf-8", errors="replace")

        return SourceViewResultV1(
            file_id=file_row.id,
            file_path=file_row.path,
            file_sha256=file_row.sha256,
            representation=SourceRepresentation.MODEL_SAFE,
            requested_range=req,
            actual_range=actual_range,
            content_bytes=sliced_bytes,
            content_text=text,
            content_sha256=sliced_sha,
            total_file_size=safe_total,
            policy_version=policy.policy_version,
            redaction_map_digest=transform_output.redaction_map_digest,
            interval_mappings=sliced_mappings,
            remaining_privacy_risks=REMAINING_PRIVACY_RISK_LIMITATIONS,
            is_omitted=False,
            omission_reason=None,
        )
