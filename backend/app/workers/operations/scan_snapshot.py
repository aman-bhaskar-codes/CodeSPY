"""Execute a stored scan specification sequentially, outside database locks."""

import asyncio
from collections.abc import Awaitable, Callable

from app.contracts.findings import ScanPublicationDraftV1
from app.contracts.jobs import AttemptLeaseV2
from app.db.time import database_now
from app.db.transaction import transaction
from app.domain.errors import DuplicateConflict
from app.ports.object_store import ObjectStore
from app.ports.scan_execution import ScanCapabilities
from app.repositories.scan_repository import ScanRepository
from app.scanners.process import NativeProcessError
from app.scanners.receipts import BrokerReceipt
from app.services.analysis_sources import locate_source
from app.services.findings import FindingMaterializer
from app.services.jobs import JobService
from app.services.scan_publication import FindingFinalizer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class ScanOperation:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        store: ObjectStore,
        capabilities: ScanCapabilities,
        finalizer: FindingFinalizer,
    ):
        self.factory, self.store = factory, store
        self.capabilities, self.finalizer = capabilities, finalizer
        self.jobs = JobService(factory)

    async def execute(
        self,
        lease: AttemptLeaseV2,
        *,
        enter_finalization: Callable[[], Awaitable[None]] | None = None,
    ) -> ScanPublicationDraftV1:
        async with transaction(self.factory) as session:
            job, ctx = await self.jobs._locked(session, lease.org_id, lease.job_id)
            await self.jobs._current(session, lease, job)
            if job["operation"] != "scan_snapshot" or ctx is None:
                raise DuplicateConflict("scan operation differs")
            repository = ScanRepository(session)
            config = await repository.get_spec(ctx, job["repository_id"], job["scan_spec_id"])
            spec = await repository.get_attempt_spec(lease)
            if config is None or (
                config.config_hash != job["config_hash"]
                or config.config.publication_spec(spec.scope, spec.repository_id, spec.captured_at)
                != spec
            ):
                raise DuplicateConflict("stored scan configuration differs")
            self.capabilities.validate(config.config)
            located = await locate_source(
                session, lease.org_id, job["repository_id"], job["snapshot_id"]
            )
            now = await database_now(session)
            forward_budget = min(600.0, (job["deadline"] - now).total_seconds() - 65)
        if forward_budget <= 0:
            raise NativeProcessError("scanner_timeout")
        try:
            async with asyncio.timeout(forward_budget):
                native = []
                for request in spec.native_requests:
                    receipt = await self.capabilities.native(request, located).scan_attested(
                        request
                    )
                    if not isinstance(receipt, BrokerReceipt):
                        raise NativeProcessError("scanner_output_invalid")
                    receipt.validate()
                    if receipt.request != request:
                        raise NativeProcessError("scanner_output_invalid")
                    if receipt.result.status not in {"completed", "partial"}:
                        raise NativeProcessError("scanner_output_invalid")
                    native.append(receipt)
                dependency, bundle = None, None
                if spec.dependency_request is not None:
                    request_dep = spec.dependency_request
                    bundle = self.capabilities.bundle(request_dep.bundle_digest)
                    dependency = await self.capabilities.dependencies(
                        request_dep, located
                    ).assess_attested(request_dep)
                draft = FindingMaterializer(
                    self.capabilities.catalog(spec.catalog_digest)
                ).materialize(spec, tuple(native), dependency=dependency, bundle=bundle)
        except TimeoutError:
            raise NativeProcessError("scanner_timeout") from None
        except ValueError:
            raise NativeProcessError("scanner_output_invalid") from None
        if enter_finalization is not None:
            await enter_finalization()
        return await self.finalizer.finish(lease, draft)
