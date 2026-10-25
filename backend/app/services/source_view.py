"""Verified SourceViewService: authorized, byte-exact, privacy-transformed source views."""

import hashlib
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
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
from app.db.time import database_now
from app.domain.errors import ScopeViolation
from app.models.derived_artifacts import derived_artifacts
from app.models.phase2_tables import PHASE2_TABLES
from app.models.tables import memberships, source_files
from app.ports.object_store import ObjectStore
from app.ports.source_reader import AdmittedSourceReadV1, SourceReaderPort, SourceReaderUnavailable
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

    def __init__(
        self, session: AsyncSession, store: ObjectStore, *, reader: SourceReaderPort | None = None
    ) -> None:
        self.s = session
        self.store = store
        self.reader = reader

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
            file_row.id,
            file_row.size_bytes,
        )

        # Revalidate authority after awaited external work, before returning any source.
        await self._verify_scope_and_representation(request, policy)

        # 5. Handle representation
        if request.representation == SourceRepresentation.ORIGINAL:
            return self._build_original_view(raw_bytes, file_row, request.byte_range, policy)

        result = await self._build_model_safe_view(raw_bytes, file_row, request, policy)
        await self._verify_scope_and_representation(request, policy)
        return result

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
        file_id: UUID,
        expected_size: int,
    ) -> bytes:
        located = await locate_source(self.s, org_id, repository_id, snapshot_id)
        selected = next((f for f in located.files if f.file_id == file_id), None)
        if (
            selected is None
            or not selected.eligible
            or (selected.path, selected.sha256, selected.size_bytes)
            != (file_path, expected_sha, expected_size)
        ):
            raise ScopeViolation("source_file_unavailable")
        if expected_size > 2_097_152:
            raise SourceReaderUnavailable("source_read_limit")
        if self.reader is None:
            raise SourceReaderUnavailable("source_reader_unavailable")
        content = await self.reader.read(
            AdmittedSourceReadV1(
                org_id=org_id,
                repository_id=repository_id,
                snapshot_id=snapshot_id,
                file_id=file_id,
                archive=located.archive,
                path=file_path,
                sha256=expected_sha,
                size_bytes=expected_size,
                strip_root=located.policy.github_strip_root,
            )
        )
        if len(content) != expected_size or hashlib.sha256(content).hexdigest() != expected_sha:
            raise SourceReaderUnavailable("source_read_rejected")
        current = await locate_source(self.s, org_id, repository_id, snapshot_id)
        if current.archive != located.archive or selected not in current.files:
            raise ScopeViolation("source_file_unavailable")
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
        query = select(derived_artifacts).where(
            derived_artifacts.c.org_id == request.org_id,
            derived_artifacts.c.snapshot_id == request.snapshot_id,
            derived_artifacts.c.file_id == request.file_id,
            derived_artifacts.c.representation == "model_safe",
            derived_artifacts.c.policy_version == policy.policy_version,
        )
        transform_output = transform_model_safe(raw_bytes, policy)
        expected = {
            "repository_id": request.repository_id,
            "source_sha256": file_row.sha256,
            "derived_sha256": hashlib.sha256(transform_output.safe_bytes).hexdigest(),
            "size_bytes": len(transform_output.safe_bytes),
            "redaction_digest": transform_output.redaction_map_digest,
            "interval_map_json": [m.model_dump() for m in transform_output.mappings],
            "transformation_status": "ready",
        }
        cached_row = (await self.s.execute(query)).mappings().one_or_none()
        if cached_row is None:
            try:
                async with self.s.begin_nested():
                    await self.s.execute(
                        derived_artifacts.insert().values(
                            org_id=request.org_id,
                            id=uuid4(),
                            snapshot_id=request.snapshot_id,
                            file_id=request.file_id,
                            representation="model_safe",
                            policy_version=policy.policy_version,
                            storage_key=None,
                            **expected,
                        )
                    )
            except IntegrityError:
                pass
            cached_row = (await self.s.execute(query)).mappings().one_or_none()
        if cached_row is None or any(cached_row[key] != value for key, value in expected.items()):
            raise SourceReaderUnavailable("source_representation_unavailable")
        expires = cached_row["expires_at"]
        if expires is not None and expires <= await database_now(self.s):
            raise SourceReaderUnavailable("source_representation_expired")

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
