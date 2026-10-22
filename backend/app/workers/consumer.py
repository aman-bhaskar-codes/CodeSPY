"""Process bounded wakeups; acknowledge only a durable or obsolete outcome."""

import asyncio
import logging
from typing import Protocol
from uuid import uuid4

from app.adapters.github.http import ProviderFailure
from app.admission.zip_inventory import AdmissionRejected
from app.analyzers.runner import AnalysisProcessError
from app.analyzers.source import SourceIntegrityError
from app.contracts.ingestion import AdmissionPolicy, ArtifactManifestV1, resolve_admission_policy
from app.contracts.jobs import AttemptLeaseV2, JobEnvelopeV2, JobEnvelopeV3
from app.contracts.scan_jobs import JobEnvelopeV4
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
from app.scanners.process import NativeProcessError
from app.services.job_finalization import JobFinalizer
from app.services.jobs import JobService
from app.workers.operations.analyze_structure import StructuralAnalysisOperation
from app.workers.operations.scan_snapshot import ScanOperation
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)


def scan_failure(error: NativeProcessError) -> tuple[str, bool]:
    return {
        "scanner_runtime_unavailable": ("scanner_unavailable", False),
        "scanner_timeout": ("scanner_timeout", False),
        "scanner_output_limit": ("scan_capacity_exceeded", False),
        "scanner_cleanup_pending": ("scanner_process_failed", True),
        "scanner_process_failed": ("scanner_process_failed", True),
        "scanner_source_invalid": ("source_integrity_error", False),
    }.get(str(error), ("scanner_output_invalid", False))


class AdmissionProcessor(Protocol):
    async def inspect(
        self, lease: AttemptLeaseV2, policy: AdmissionPolicy
    ) -> ArtifactManifestV1: ...


class JobConsumer:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        queue: QueuePort,
        finalizer: JobFinalizer,
        processor: AdmissionProcessor,
        structural_operation: StructuralAnalysisOperation | None = None,
        scan_operation: ScanOperation | None = None,
    ) -> None:
        self.factory = factory
        self.queue = queue
        self.finalizer = finalizer
        self.processor = processor
        self.structural_operation = structural_operation
        self.scan_operation = scan_operation
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
        envelope: JobEnvelopeV2 | JobEnvelopeV3 | JobEnvelopeV4
        try:
            envelope = JobEnvelopeV4.model_validate_json(delivery.body)
        except ValidationError:
            try:
                envelope = JobEnvelopeV3.model_validate_json(delivery.body)
            except ValidationError:
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
            or (
                isinstance(envelope, JobEnvelopeV4)
                and job.get("scan_spec_id") != envelope.scan_spec_id
            )
        ):
            logger.warning("mismatched_queue_envelope")
            await self._ack(delivery.receipt)
            return
        if envelope.operation == "scan_snapshot" and self.scan_operation is None:
            await self.queue.extend(delivery.receipt, 120)
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
                try:
                    policy = resolve_admission_policy(
                        job["config_hash"], github_strip_root=source_kind == "github_archive"
                    )
                except ValueError:
                    raise DuplicateConflict("unsupported admission policy") from None
                manifest = await self.processor.inspect(lease, policy)
                prepared = await self.finalizer.prepare_admission(lease, manifest)
                await self.finalizer.finish_admission(
                    lease, prepared, before_lock=enter_finalization
                )
            elif envelope.operation == "inventory_snapshot":
                batch = await self.finalizer.inventory_batch(lease)
                await enter_finalization()
                await self.finalizer.finish_inventory(lease, batch)
            elif envelope.operation == "analyze_structure":
                if self.structural_operation is None:
                    raise RuntimeError("structural analysis operation not configured on consumer")
                await self.structural_operation.execute(
                    lease, enter_finalization=enter_finalization
                )
            elif envelope.operation == "scan_snapshot":
                assert self.scan_operation is not None
                await self.scan_operation.execute(lease, enter_finalization=enter_finalization)
            else:
                raise ValueError(f"unknown operation {envelope.operation}")

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
        except NativeProcessError as error:
            code, transient = scan_failure(error)
            await self.jobs.fail_attempt(lease, code, transient=transient)
            await self._ack(delivery.receipt)
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
        except (AnalysisProcessError, SourceIntegrityError) as error:
            code = str(error)
            if code not in {
                "analysis_timeout",
                "analyzer_output_invalid",
                "source_integrity_error",
                "analysis_unavailable",
            }:
                code = "analyzer_output_invalid"
            await self.jobs.fail_attempt(lease, code, transient=False)
            await self._ack(delivery.receipt)
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
        except ValueError:
            await self.jobs.fail_attempt(
                lease,
                "scanner_output_invalid"
                if envelope.operation == "scan_snapshot"
                else "integrity_failure",
                transient=False,
            )
            await self._ack(delivery.receipt)
        except Exception:
            await self.jobs.fail_attempt(
                lease,
                "scanner_process_failed"
                if envelope.operation == "scan_snapshot"
                else "analysis_process_failed"
                if envelope.operation == "analyze_structure"
                else "admission_process_failed",
                transient=True,
            )
            await self._ack(delivery.receipt)
        finally:
            stop.set()
            if not operation.done():
                operation.cancel()
            await asyncio.gather(operation, heartbeat, return_exceptions=True)
