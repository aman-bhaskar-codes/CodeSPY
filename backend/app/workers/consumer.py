"""Process bounded wakeups; acknowledge only a durable or obsolete outcome."""

import asyncio
import logging
from typing import Protocol
from uuid import uuid4

from app.adapters.github.http import ProviderFailure
from app.admission.zip_inventory import AdmissionRejected
from app.contracts.ingestion import AdmissionPolicyV1, ArtifactManifestV1
from app.contracts.jobs import AttemptLeaseV2, JobEnvelopeV2
from app.db.transaction import transaction
from app.domain.errors import (
    DuplicateConflict,
    FinalizationOutcomeUnknown,
    ScopeViolation,
    StaleAttempt,
)
from app.models.phase2_tables import PHASE2_TABLES
from app.ports.queue import Delivery, QueuePort
from app.repositories.job_repository import load_job
from app.services.job_finalization import JobFinalizer
from app.services.jobs import JobService
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)


class AdmissionProcessor(Protocol):
    async def inspect(
        self, lease: AttemptLeaseV2, policy: AdmissionPolicyV1
    ) -> ArtifactManifestV1: ...


class JobConsumer:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        queue: QueuePort,
        finalizer: JobFinalizer,
        processor: AdmissionProcessor,
    ) -> None:
        self.factory = factory
        self.queue = queue
        self.finalizer = finalizer
        self.processor = processor
        self.jobs = JobService(factory)

    async def _ack(self, receipt: str) -> None:
        try:
            await self.queue.ack(receipt)
        except Exception:
            logger.warning("queue_ack_unavailable")

    async def handle(self, delivery: Delivery) -> None:
        if len(delivery.body) > 4096:
            logger.warning("invalid_queue_envelope")
            await self._ack(delivery.receipt)
            return
        try:
            envelope = JobEnvelopeV2.model_validate_json(delivery.body)
        except ValidationError:
            logger.warning("invalid_queue_envelope")
            await self._ack(delivery.receipt)
            return
        async with transaction(self.factory) as s:
            job = await load_job(s, envelope.org_id, envelope.job_id)
            source_kind = (
                await s.scalar(
                    select(PHASE2_TABLES["source_submissions"].c.source_kind).where(
                        PHASE2_TABLES["source_submissions"].c.org_id == envelope.org_id,
                        PHASE2_TABLES["source_submissions"].c.id == job["submission_id"],
                    )
                )
                if job["submission_id"]
                else None
            )
        if (
            job["operation"] != envelope.operation
            or job["config_hash"] != envelope.config_hash
            or job["deadline"] != envelope.deadline
        ):
            logger.warning("mismatched_queue_envelope")
            await self._ack(delivery.receipt)
            return
        lease = await self.jobs.claim(
            envelope.org_id, envelope.job_id, envelope.dispatch_generation, uuid4().hex
        )
        if not isinstance(lease, AttemptLeaseV2):
            if lease in {"obsolete", "already_leased", "unavailable"}:
                await self._ack(delivery.receipt)
            else:
                await self.queue.extend(delivery.receipt, 5)
            return
        stop = asyncio.Event()
        finalizing = asyncio.Event()
        lost = asyncio.Event()
        lease_serial = asyncio.Lock()

        async def enter_finalization() -> None:
            async with lease_serial:
                await self.jobs.heartbeat(lease)
                finalizing.set()

        async def operate() -> None:
            if envelope.operation == "admit_source":
                manifest = await self.processor.inspect(
                    lease, AdmissionPolicyV1(github_strip_root=source_kind == "github_archive")
                )
                prepared = await self.finalizer.prepare_admission(lease, manifest)
                await self.finalizer.finish_admission(
                    lease, prepared, before_lock=enter_finalization
                )
            else:
                batch = await self.finalizer.inventory_batch(lease)
                await enter_finalization()
                await self.finalizer.finish_inventory(lease, batch)

        operation = asyncio.create_task(operate())

        async def maintain() -> None:
            while not stop.is_set():
                try:
                    async with asyncio.timeout(20):
                        await stop.wait()
                    return
                except TimeoutError:
                    pass
                if finalizing.is_set():
                    continue
                try:
                    async with lease_serial:
                        if finalizing.is_set():
                            continue
                        await self.jobs.heartbeat(lease)
                except Exception:
                    lost.set()
                    operation.cancel()
                    return
                try:
                    await self.queue.extend(delivery.receipt, 120)
                except Exception:
                    logger.warning("queue_visibility_unavailable")

        heartbeat = asyncio.create_task(maintain())
        try:
            await operation
            await self._ack(delivery.receipt)
        except asyncio.CancelledError:
            if not lost.is_set():
                raise
        except StaleAttempt:
            pass
        except FinalizationOutcomeUnknown:
            logger.warning("finalization_outcome_unknown")
        except ProviderFailure as error:
            code = (
                error.code
                if error.code in {"access_revoked", "archive_limit_exceeded"}
                else "provider_unavailable"
            )
            try:
                await self.jobs.fail_attempt(lease, code, transient=error.transient)
            except (StaleAttempt, ScopeViolation):
                return
            await self._ack(delivery.receipt)
        except ScopeViolation:
            # Local authority revocation is terminalized by independent recovery.
            logger.warning("worker_authority_unavailable")
        except DuplicateConflict:
            await self.jobs.fail_attempt(lease, "integrity_failure")
            await self._ack(delivery.receipt)
        except AdmissionRejected as error:
            code = str(error)
            if code not in {
                "unsafe_archive_path",
                "archive_corrupt",
                "archive_limit_exceeded",
                "unsupported_format",
            }:
                code = "invalid_archive"
            await self.jobs.fail_attempt(lease, code)
            await self._ack(delivery.receipt)
        except Exception:
            await self.jobs.fail_attempt(lease, "admission_process_failed", transient=True)
            await self._ack(delivery.receipt)
        finally:
            stop.set()
            if not operation.done():
                operation.cancel()
            await asyncio.gather(operation, heartbeat, return_exceptions=True)
