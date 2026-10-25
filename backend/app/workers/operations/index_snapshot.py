"""Prepare safe lexical content outside database locks, then publish under fresh authority."""

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from pathlib import Path

from app.contracts.indexing import IndexReceiptV1
from app.contracts.jobs import AttemptLeaseV2
from app.db.time import database_now
from app.db.transaction import transaction
from app.domain.errors import DuplicateConflict
from app.indexing.staging import IndexInputStaging
from app.ports.index_builder import IndexBuilderPort, IndexBuilderUnavailable, VerifiedIndexInputV1
from app.ports.object_store import ObjectStore
from app.ports.source_reader import AdmittedSourceReadV1, SourceReaderPort, SourceReaderUnavailable
from app.scanners.process import NativeProcessError
from app.services.index_publication import IndexFinalizer, current_input
from app.services.jobs import JobService
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class IndexOperation:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        store: ObjectStore,
        reader: SourceReaderPort,
        builder: IndexBuilderPort,
        finalizer: IndexFinalizer,
        *,
        verified_root: Path,
    ):
        if (
            not verified_root.is_absolute()
            or not verified_root.is_dir()
            or verified_root.resolve() != verified_root
        ):
            raise ValueError("owned verified input directory required")
        self.factory, self.store, self.reader = factory, store, reader
        self.builder, self.finalizer, self.verified_root = builder, finalizer, verified_root
        self.jobs = JobService(factory)
        self.staging = IndexInputStaging(verified_root)

    async def execute(
        self,
        lease: AttemptLeaseV2,
        *,
        enter_finalization: Callable[[], Awaitable[None]] | None = None,
    ) -> IndexReceiptV1:
        async with transaction(self.factory) as session:
            job, _, spec = await current_input(session, self.jobs, lease)
            now = await database_now(session)
            seconds = min(180.0, (job["deadline"] - now).total_seconds() - 65)
        if seconds <= 0:
            raise NativeProcessError("index_timeout")
        deadline = asyncio.get_running_loop().time() + seconds
        try:
            async with asyncio.timeout_at(deadline):
                capability = await self.builder.capabilities()
                if capability.runtime_source_sha256 != spec.config.runtime_source_sha256:
                    raise IndexBuilderUnavailable("index_builder_unavailable")
                async with self.staging.open(spec.config.runtime_image) as directory:
                    inputs = []
                    for file in spec.files:
                        raw = await self.reader.read(
                            AdmittedSourceReadV1(
                                org_id=spec.org_id,
                                repository_id=spec.repository_id,
                                snapshot_id=spec.snapshot_id,
                                file_id=file.file_id,
                                archive=spec.source,
                                path=file.path,
                                sha256=file.sha256,
                                size_bytes=file.size_bytes,
                                strip_root=spec.strip_root,
                            )
                        )
                        if (
                            len(raw) != file.size_bytes
                            or hashlib.sha256(raw).hexdigest() != file.sha256
                        ):
                            raise DuplicateConflict("admitted index source differs")
                        path = Path(directory) / str(file.file_id)
                        with path.open("xb") as stream:
                            stream.write(raw)
                        path.chmod(0o600)
                        inputs.append(VerifiedIndexInputV1(file=file, owned_path=path))
                        del raw
                    async with transaction(self.factory) as session:
                        _, _, current = await current_input(session, self.jobs, lease)
                        if current != spec:
                            raise DuplicateConflict("index input changed during preparation")
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise TimeoutError()
                    prepared = await self.builder.build(
                        spec, tuple(inputs), timeout_seconds=remaining
                    )
                    prepared.validate()
                    if prepared.spec != spec:
                        raise DuplicateConflict("index runtime specification differs")
                    if asyncio.get_running_loop().time() >= deadline:
                        raise TimeoutError()
        except TimeoutError:
            raise NativeProcessError("index_timeout") from None
        except IndexBuilderUnavailable as error:
            raise NativeProcessError(error.failure_code) from None
        except SourceReaderUnavailable as error:
            if str(error) == "source_read_rejected":
                raise DuplicateConflict("admitted index source differs") from None
            code = (
                "index_cleanup_pending"
                if str(error) == "source_cleanup_pending"
                else "storage_unavailable"
            )
            raise NativeProcessError(code) from None
        if enter_finalization is not None:
            await enter_finalization()
        return await self.finalizer.finish(lease, prepared)

    async def recover_owned(self) -> None:
        """Exclusive startup recovery precedes queue consumption."""
        await self.builder.recover_owned()
        await self.staging.recover_owned()
