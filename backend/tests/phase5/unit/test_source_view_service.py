"""Unit tests for SourceViewService verification, privacy transformations, and authorization."""

import hashlib
import io
import zipfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import patch
from uuid import uuid4

import pytest
import pytest_asyncio
from app.contracts.ingestion import AdmissionPolicyV1, ObjectDescriptor
from app.contracts.source_view import (
    ByteRangeV1,
    PrivacyPolicyV1,
    SourceRepresentation,
    SourceViewRequestV1,
)
from app.contracts.structure import SourceFileV1
from app.domain.errors import ScopeViolation
from app.models.derived_artifacts import derived_artifacts
from app.models.phase2_tables import PHASE2_TABLES, md
from app.models.tables import memberships, source_files
from app.ports.object_store import ObjectStore
from app.ports.source_reader import AdmittedSourceReadV1
from app.services.analysis_sources import LocatedSource
from app.services.source_view import SourceViewService
from app.source_read.extract import read_admitted_entry
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


class MemoryObjectStore(ObjectStore):
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    async def put_stage(self, key: str, chunks: AsyncIterator[bytes], max_bytes: int):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def put_if_absent(self, key: str, source: ObjectDescriptor) -> ObjectDescriptor:
        raise NotImplementedError

    async def open_verified(self, descriptor: ObjectDescriptor) -> AsyncIterator[bytes]:
        data = self.objects[descriptor.key]
        yield data

    async def delete_exact(self, descriptor: ObjectDescriptor) -> None:
        pass

    async def describe_owned(self, key: str, max_bytes: int):  # type: ignore[no-untyped-def]
        return None


class FixtureSourceReader:
    """In-process test double, never a production source reader."""

    def __init__(self, store: MemoryObjectStore):
        self.store = store

    async def read(self, request: AdmittedSourceReadV1) -> bytes:
        return read_admitted_entry(
            self.store.objects[request.archive.key],
            path=request.path,
            sha256=request.sha256,
            size_bytes=request.size_bytes,
            strip_root=request.strip_root,
        )


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    from sqlalchemy import CheckConstraint

    source_files.c.path.type.collation = None  # type: ignore[attr-defined]
    for c in list(source_files.constraints):
        if isinstance(c, CheckConstraint):
            source_files.constraints.discard(c)

    grants = PHASE2_TABLES["repository_grants"]
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: md.create_all(
                sync_conn,
                tables=[memberships, grants, source_files, derived_artifacts],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


def make_test_archive(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, data in files.items():
            zf.writestr(path, data)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_model_safe_view_redacts_canary_and_registers_derived_artifact(
    session: AsyncSession,
) -> None:
    org_id = uuid4()
    principal_id = uuid4()
    repo_id = uuid4()
    snapshot_id = uuid4()
    file_id = uuid4()

    canary = "codespy_sec_canary_alpha_view"
    code = f"def compute():\n    api_key = '{canary}'\n    return 42\n".encode()
    code_sha = hashlib.sha256(code).hexdigest()

    # 1. Populate DB fixtures
    await session.execute(
        memberships.insert().values(org_id=org_id, principal_id=principal_id, role="member")
    )
    grants = PHASE2_TABLES["repository_grants"]
    await session.execute(
        grants.insert().values(
            org_id=org_id,
            repository_id=repo_id,
            principal_id=principal_id,
            permission="read",
            revoked_at=None,
        )
    )
    await session.execute(
        source_files.insert().values(
            org_id=org_id,
            snapshot_id=snapshot_id,
            id=file_id,
            path="src/compute.py",
            sha256=code_sha,
            size_bytes=len(code),
        )
    )
    await session.commit()

    archive_bytes = make_test_archive({"src/compute.py": code})
    archive_sha = hashlib.sha256(archive_bytes).hexdigest()
    store = MemoryObjectStore({"archive.zip": archive_bytes})

    archive_desc = ObjectDescriptor(
        key="archive.zip",
        sha256=archive_sha,
        size_bytes=len(archive_bytes),
    )
    located = LocatedSource(
        archive=archive_desc,
        manifest=archive_desc,
        files=(
            SourceFileV1(
                file_id=file_id,
                path="src/compute.py",
                sha256=code_sha,
                size_bytes=len(code),
                eligible=True,
            ),
        ),
        policy=AdmissionPolicyV1(),
        revision="main",
    )

    svc = SourceViewService(session, store, reader=FixtureSourceReader(store))
    req = SourceViewRequestV1(
        org_id=org_id,
        repository_id=repo_id,
        snapshot_id=snapshot_id,
        principal_id=principal_id,
        file_id=file_id,
        representation=SourceRepresentation.MODEL_SAFE,
    )
    policy = PrivacyPolicyV1()

    with patch("app.services.source_view.locate_source", return_value=located):
        res = await svc.get(req, policy)

    assert res.content_text is not None
    assert canary not in res.content_text
    assert canary.encode() not in res.content_bytes
    assert "[REDACTED_SECRET:canary]" in res.content_text
    assert res.representation == SourceRepresentation.MODEL_SAFE
    assert res.is_omitted is False
    assert len(res.interval_mappings) > 0

    # Verify registered in derived_artifacts
    rows = (
        (
            await session.execute(
                select(derived_artifacts).where(
                    derived_artifacts.c.org_id == org_id,
                    derived_artifacts.c.file_id == file_id,
                )
            )
        )
        .mappings()
        .all()
    )
    assert len(rows) == 1
    assert rows[0]["transformation_status"] == "ready"


@pytest.mark.asyncio
async def test_sensitive_path_omitted_under_model_safe(session: AsyncSession) -> None:
    org_id = uuid4()
    principal_id = uuid4()
    repo_id = uuid4()
    snapshot_id = uuid4()
    file_id = uuid4()

    env_content = b"DATABASE_URL=postgres://user:pass@localhost/db\n"
    env_sha = hashlib.sha256(env_content).hexdigest()

    await session.execute(
        memberships.insert().values(org_id=org_id, principal_id=principal_id, role="member")
    )
    grants = PHASE2_TABLES["repository_grants"]
    await session.execute(
        grants.insert().values(
            org_id=org_id,
            repository_id=repo_id,
            principal_id=principal_id,
            permission="read",
            revoked_at=None,
        )
    )
    await session.execute(
        source_files.insert().values(
            org_id=org_id,
            snapshot_id=snapshot_id,
            id=file_id,
            path=".env",
            sha256=env_sha,
            size_bytes=len(env_content),
        )
    )
    await session.commit()

    store = MemoryObjectStore({})
    svc = SourceViewService(session, store, reader=FixtureSourceReader(store))
    req = SourceViewRequestV1(
        org_id=org_id,
        repository_id=repo_id,
        snapshot_id=snapshot_id,
        principal_id=principal_id,
        file_id=file_id,
        representation=SourceRepresentation.MODEL_SAFE,
    )
    policy = PrivacyPolicyV1()

    res = await svc.get(req, policy)
    assert res.is_omitted is True
    assert res.omission_reason == "sensitive_path_exclusion"
    assert res.content_bytes == b""
    assert res.content_text == ""


@pytest.mark.asyncio
async def test_original_representation_authorization(session: AsyncSession) -> None:
    org_id = uuid4()
    member_id = uuid4()
    viewer_id = uuid4()
    repo_id = uuid4()
    snapshot_id = uuid4()
    file_id = uuid4()

    canary = "codespy_sec_canary_secret_original"
    code = f"secret = '{canary}'\n".encode()
    code_sha = hashlib.sha256(code).hexdigest()

    await session.execute(
        memberships.insert().values(
            [
                {"org_id": org_id, "principal_id": member_id, "role": "member"},
                {"org_id": org_id, "principal_id": viewer_id, "role": "viewer"},
            ]
        )
    )
    grants = PHASE2_TABLES["repository_grants"]
    await session.execute(
        grants.insert().values(
            [
                {
                    "org_id": org_id,
                    "repository_id": repo_id,
                    "principal_id": member_id,
                    "permission": "read",
                    "revoked_at": None,
                },
                {
                    "org_id": org_id,
                    "repository_id": repo_id,
                    "principal_id": viewer_id,
                    "permission": "read",
                    "revoked_at": None,
                },
            ]
        )
    )
    await session.execute(
        source_files.insert().values(
            org_id=org_id,
            snapshot_id=snapshot_id,
            id=file_id,
            path="src/token.py",
            sha256=code_sha,
            size_bytes=len(code),
        )
    )
    await session.commit()

    archive_bytes = make_test_archive({"src/token.py": code})
    store = MemoryObjectStore({"archive.zip": archive_bytes})
    desc = ObjectDescriptor(
        key="archive.zip",
        sha256=hashlib.sha256(archive_bytes).hexdigest(),
        size_bytes=len(archive_bytes),
    )
    located = LocatedSource(
        archive=desc,
        manifest=desc,
        files=(
            SourceFileV1(
                file_id=file_id,
                path="src/token.py",
                sha256=code_sha,
                size_bytes=len(code),
                eligible=True,
            ),
        ),
        policy=AdmissionPolicyV1(),
        revision="main",
    )

    svc = SourceViewService(session, store, reader=FixtureSourceReader(store))

    # 1. Allowed for member when policy permits
    req_member = SourceViewRequestV1(
        org_id=org_id,
        repository_id=repo_id,
        snapshot_id=snapshot_id,
        principal_id=member_id,
        file_id=file_id,
        representation=SourceRepresentation.ORIGINAL,
    )
    policy_allowed = PrivacyPolicyV1(allow_original_view=True)
    with patch("app.services.source_view.locate_source", return_value=located):
        res = await svc.get(req_member, policy_allowed)
    assert res.content_text is not None
    assert canary in res.content_text
    assert res.representation == SourceRepresentation.ORIGINAL

    # 2. Denied when policy disallows
    policy_denied = PrivacyPolicyV1(allow_original_view=False)
    with pytest.raises(ScopeViolation, match="denies original unredacted"):
        await svc.get(req_member, policy_denied)

    # 3. Denied for viewer role even when policy allows
    req_viewer = SourceViewRequestV1(
        org_id=org_id,
        repository_id=repo_id,
        snapshot_id=snapshot_id,
        principal_id=viewer_id,
        file_id=file_id,
        representation=SourceRepresentation.ORIGINAL,
    )
    with pytest.raises(ScopeViolation, match="Viewer role is not authorized"):
        await svc.get(req_viewer, policy_allowed)


@pytest.mark.asyncio
async def test_sliced_range_query(session: AsyncSession) -> None:
    org_id = uuid4()
    principal_id = uuid4()
    repo_id = uuid4()
    snapshot_id = uuid4()
    file_id = uuid4()

    code = b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    code_sha = hashlib.sha256(code).hexdigest()

    await session.execute(
        memberships.insert().values(org_id=org_id, principal_id=principal_id, role="member")
    )
    grants = PHASE2_TABLES["repository_grants"]
    await session.execute(
        grants.insert().values(
            org_id=org_id,
            repository_id=repo_id,
            principal_id=principal_id,
            permission="read",
            revoked_at=None,
        )
    )
    await session.execute(
        source_files.insert().values(
            org_id=org_id,
            snapshot_id=snapshot_id,
            id=file_id,
            path="src/alpha.txt",
            sha256=code_sha,
            size_bytes=len(code),
        )
    )
    await session.commit()

    archive_bytes = make_test_archive({"src/alpha.txt": code})
    desc = ObjectDescriptor(
        key="archive.zip",
        sha256=hashlib.sha256(archive_bytes).hexdigest(),
        size_bytes=len(archive_bytes),
    )
    located = LocatedSource(
        archive=desc,
        manifest=desc,
        files=(
            SourceFileV1(
                file_id=file_id,
                path="src/alpha.txt",
                sha256=code_sha,
                size_bytes=len(code),
                eligible=True,
            ),
        ),
        policy=AdmissionPolicyV1(),
        revision="main",
    )
    store = MemoryObjectStore({"archive.zip": archive_bytes})
    svc = SourceViewService(session, store, reader=FixtureSourceReader(store))

    req = SourceViewRequestV1(
        org_id=org_id,
        repository_id=repo_id,
        snapshot_id=snapshot_id,
        principal_id=principal_id,
        file_id=file_id,
        byte_range=ByteRangeV1(start_byte=10, end_byte=20),
        representation=SourceRepresentation.ORIGINAL,
    )
    policy = PrivacyPolicyV1(allow_original_view=True)
    with patch("app.services.source_view.locate_source", return_value=located):
        res = await svc.get(req, policy)

    assert res.actual_range == ByteRangeV1(start_byte=10, end_byte=20)
    assert res.content_bytes == b"ABCDEFGHIJ"


@pytest.mark.asyncio
async def test_revoked_grant_denies_view(session: AsyncSession) -> None:
    org_id = uuid4()
    principal_id = uuid4()
    repo_id = uuid4()
    snapshot_id = uuid4()
    file_id = uuid4()

    await session.execute(
        memberships.insert().values(org_id=org_id, principal_id=principal_id, role="member")
    )
    grants = PHASE2_TABLES["repository_grants"]
    await session.execute(
        grants.insert().values(
            org_id=org_id,
            repository_id=repo_id,
            principal_id=principal_id,
            permission="read",
            revoked_at=datetime.now(UTC),  # Revoked grant
        )
    )
    await session.commit()

    store = MemoryObjectStore({})
    svc = SourceViewService(session, store, reader=FixtureSourceReader(store))
    req = SourceViewRequestV1(
        org_id=org_id,
        repository_id=repo_id,
        snapshot_id=snapshot_id,
        principal_id=principal_id,
        file_id=file_id,
    )
    with pytest.raises(ScopeViolation, match="missing or revoked"):
        await svc.get(req, PrivacyPolicyV1())
