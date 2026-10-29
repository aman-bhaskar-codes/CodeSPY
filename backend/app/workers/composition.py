"""Explicit worker startup composition; deployment supplies configured ports."""

from app.ports.object_store import ObjectStore
from app.ports.queue import QueuePort
from app.services.investigation_runtime import build_investigation_operation
from app.services.investigation_tools import InvestigationToolService
from app.services.job_finalization import JobFinalizer
from app.services.model_call_results import SqlModelResultStore
from app.services.model_runtime import ConfiguredModelRuntime
from app.workers.consumer import AdmissionProcessor, JobConsumer
from app.workers.dispatcher import OutboxDispatcher
from app.workers.operations.analyze_structure import StructuralAnalysisOperation
from app.workers.operations.index_snapshot import IndexOperation
from app.workers.operations.scan_snapshot import ScanOperation
from app.workers.runtime import WorkerRuntime
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def build_worker_runtime(
    *,
    factory: async_sessionmaker[AsyncSession],
    queue: QueuePort,
    store: ObjectStore,
    admission: AdmissionProcessor,
    model_runtime: ConfiguredModelRuntime | None = None,
    structural: StructuralAnalysisOperation | None = None,
    scan: ScanOperation | None = None,
    index: IndexOperation | None = None,
    propose_report_memory: bool = False,
) -> WorkerRuntime:
    """Connect registered operations and maintenance without reading credentials."""
    investigation = None
    if model_runtime is not None:
        if model_runtime.continuations is None:
            raise ValueError("investigation startup requires encrypted continuation storage")
        tools = InvestigationToolService(
            factory,
            source_reader=model_runtime.source_reader,
            store=store,
            memory_context=model_runtime.memory_context,
            scan_capabilities=model_runtime.scan_capabilities,
        )
        investigation = build_investigation_operation(
            runtime=model_runtime,
            worker_factory=factory,
            tools=tools,
            result_store=SqlModelResultStore(
                model_runtime.accounting_factory, model_runtime.continuations.cipher
            ),
            propose_report_memory=propose_report_memory,
        )
    elif propose_report_memory:
        raise ValueError("report proposals require a configured model runtime")
    consumer = JobConsumer(
        factory,
        queue,
        JobFinalizer(factory, store),
        admission,
        structural_operation=structural,
        scan_operation=scan,
        index_operation=index,
        investigation_operation=investigation,
    )
    return WorkerRuntime(
        OutboxDispatcher(factory, queue),
        consumer,
        model_maintenance=model_runtime.maintenance_tick if model_runtime else None,
    )
