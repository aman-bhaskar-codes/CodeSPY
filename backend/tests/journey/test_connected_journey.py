"""Complete connected local journey qualification:

Exercises the full, end-to-end single journey from upload and admission
through structural analysis, security scanning, exact/dense retrieval,
reviewed-memory promotion, and saved investigation execution against
real PostgreSQL and isolated Docker runtimes.
"""

from __future__ import annotations

import io
import json
import os
import zipfile
from uuid import uuid4

import httpx
import pytest
from app.adapters.model.http_transport import HTTPModelTransport
from app.adapters.model.openai import OpenAIResponsesCodec
from app.adapters.queue.memory import MemoryQueue
from app.adapters.storage.local import LocalObjectStore
from app.admission.zip_inventory import inspect_zip_bytes
from app.analyzers.pipeline import analyze
from app.analyzers.positions import SourceIndex
from app.contracts.context import ContextAssemblyRequestV1, ContextBudgetV1
from app.contracts.indexing import IndexBuildConfigV1
from app.contracts.ingestion import AdmissionPolicyV1, UploadReservationV1
from app.contracts.investigation import InvestigationDepth, InvestigationMode
from app.contracts.investigation_execution import (
    InvestigationAdvanceStatus,
)
from app.contracts.investigation_jobs import InvestigationCreateRequestV2
from app.contracts.jobs import AttemptLeaseV2
from app.contracts.memory import (
    EvidencePointerV1,
    MemoryCreateRequestV1,
    MemoryKind,
    MemoryQueryRequestV1,
    MemoryReviewRequestV1,
)
from app.contracts.model_configuration import ModelRegistryConfigV1, ModelTaskRouteV1
from app.contracts.scan_jobs import NativeSelectionV1, ScanConfigV1
from app.contracts.scanners import (
    ScannerFileV1,
    ScannerObservationV1,
    ScannerProfileV1,
    ScannerResultV1,
)
from app.contracts.search import (
    ExactSearchRequestV1,
    HybridSearchRequestV1,
    LexicalSearchRequestV1,
    SearchScopeV1,
)
from app.contracts.structure import AnalysisConfigV1
from app.indexing.adapter import DockerIndexBuilder
from app.indexing.docker import IndexDockerController
from app.models.investigation_execution_tables import (
    investigation_evidence,
    investigation_execution_checkpoints,
    investigation_reports,
)
from app.models.memory_tables import repo_memories
from app.models.model_gateway_tables import model_spend_ledger
from app.models.phase3_tables import structural_reports
from app.models.tables import (
    cpg_nodes,
    evidence_records,
    memberships,
    source_files,
    source_snapshots,
)
from app.scanners.docker import DockerController
from app.scanners.finding_catalog import reviewed_catalog
from app.scanners.ownership import ScannerOwnership
from app.scanners.receipts import _issue as _issue_scan
from app.security.authorization import require_repository
from app.services.analysis_sources import locate_source
from app.services.context_assembly import ContextAssemblyService
from app.services.hybrid_search import HybridSearchService
from app.services.index_jobs import IndexJobService
from app.services.index_publication import IndexFinalizer
from app.services.investigation_jobs import InvestigationJobService
from app.services.job_finalization import JobFinalizer
from app.services.jobs import JobService
from app.services.lexical_search import LexicalSearchService
from app.services.memory_service import MemoryService
from app.services.model_registry import ModelRegistry
from app.services.model_runtime import ConfiguredModelRuntime
from app.services.scan_jobs import ScanJobService
from app.services.scan_publication import FindingFinalizer
from app.services.scan_reads import ScanReadService
from app.services.structural_analysis import StructuralAnalysisService
from app.services.structural_finalization import StructuralFinalizer
from app.services.submissions import SubmissionService
from app.source_read.adapter import DockerSourceReader
from app.workers.composition import build_worker_runtime
from app.workers.operations.analyze_structure import StructuralAnalysisOperation
from app.workers.operations.index_snapshot import IndexOperation
from app.workers.operations.scan_snapshot import ScanOperation
from pydantic import SecretStr
from sqlalchemy import func, select
from tests.model_connections.storage.test_configured_gateway import configured
from tests.phase2.integration.test_submission_ingestion import chunks
from tests.phase4.storage.test_scan_jobs import Registry

pytestmark = [
    pytest.mark.schema_revision("0040_investigation_runtime_permissions"),
    pytest.mark.skipif(
        not os.environ.get("CODESPY_TEST_MARKER"),
        reason="owned disposable PostgreSQL environment required",
    ),
]


AUTH_PY_CONTENT = (
    "import subprocess\n\n"
    'canary = "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"\n\n'
    "def execute_user_command(cmd: str) -> None:\n"
    '    """Execute system command on behalf of authenticated user."""\n'
    "    subprocess.run(cmd, shell=True)\n\n"
    "def login(user: str, token: str) -> bool:\n"
    "    return token == canary\n\n"
    "def logout(session_id: str) -> None:\n"
    "    pass\n"
)

DB_PY_CONTENT = 'def get_db():\n    return "connected"\n'
REQUIREMENTS_CONTENT = "fastapi==0.110.0\npydantic==2.6.4\n"


def make_journey_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("app/auth.py", AUTH_PY_CONTENT.encode("utf-8"))
        z.writestr("app/db.py", DB_PY_CONTENT.encode("utf-8"))
        z.writestr("requirements.txt", REQUIREMENTS_CONTENT.encode("utf-8"))
    return buf.getvalue()


class FixtureRunner:
    async def analyze(self, source, root):
        return analyze(source, root)


async def test_complete_connected_local_journey(
    migration_factory,
    api_factory,
    broker_factory,
    tmp_path,
    docker_runtime_paths,
):
    reader_image = os.environ.get("CODESPY_SOURCE_READER_IMAGE")
    indexer_image = os.environ.get("CODESPY_INDEXER_IMAGE")
    source_hash = os.environ.get("CODESPY_INDEXER_SOURCE_HASH")
    if not (reader_image and indexer_image and source_hash):
        pytest.skip("owned isolated Docker reader and indexer images required")

    # -------------------------------------------------------------------------
    # Baseline setup: configure tenant environment and model pricing in Postgres
    # -------------------------------------------------------------------------
    gateway, args, prior_client, _ = await configured(migration_factory, tmp_path)
    await prior_client.aclose()
    repo_id = args["repository_id"]

    async with migration_factory() as session:
        ctx = await require_repository(
            session, args["org_id"], args["principal_id"], repo_id, write=True
        )

    store = LocalObjectStore(tmp_path / "journey-objects")

    # =========================================================================
    # Phase 2: Upload & Source Admission
    # =========================================================================
    body = make_journey_zip()
    sub_service = SubmissionService(api_factory, store)
    reservation = UploadReservationV1(
        repository_id=repo_id, filename="journey_source.zip", declared_size=len(body)
    )
    submission = await sub_service.reserve(ctx, reservation, uuid4().hex)
    uploaded = await sub_service.write_content(ctx, submission.id, chunks(body))
    assert uploaded.state == "admission_queued" and uploaded.job_id

    adm_lease = await JobService(broker_factory).claim(
        ctx.org_id, uploaded.job_id, 1, "admission-worker"
    )
    assert isinstance(adm_lease, AttemptLeaseV2)
    finalizer = JobFinalizer(broker_factory, store)
    prepared_adm = await finalizer.prepare_admission(
        adm_lease, inspect_zip_bytes(body, AdmissionPolicyV1())
    )
    result_adm = await finalizer.finish_admission(adm_lease, prepared_adm)
    snapshot_id = result_adm.snapshot_id

    # Verify PostgreSQL admission tables
    async with broker_factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(source_snapshots)
                .where(source_snapshots.c.id == snapshot_id)
            )
            == 1
        )
        files = (
            (
                await session.execute(
                    select(source_files.c.path).where(source_files.c.snapshot_id == snapshot_id)
                )
            )
            .scalars()
            .all()
        )
        assert "app/auth.py" in files
        assert "app/db.py" in files
        assert "requirements.txt" in files

    # =========================================================================
    # Phase 3: Structural Analysis
    # =========================================================================
    struct_config = AnalysisConfigV1(toolchain_hash="a" * 64)
    _, struct_job = await StructuralAnalysisService(api_factory).create_analysis_spec(
        ctx, repo_id, snapshot_id, struct_config, "journey-structure"
    )
    struct_lease = await JobService(broker_factory).claim(
        ctx.org_id, struct_job.id, 1, "structural-worker"
    )
    assert isinstance(struct_lease, AttemptLeaseV2)

    struct_report = await StructuralAnalysisOperation(
        broker_factory, store, StructuralFinalizer(broker_factory, store), FixtureRunner()
    ).execute(struct_lease)
    assert struct_report.coverage.syntax == "complete"
    assert struct_report.coverage.graph == "complete"

    # Verify structural analysis in PostgreSQL
    async with broker_factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(structural_reports)
                .where(structural_reports.c.snapshot_id == snapshot_id)
            )
            == 1
        )
        symbols = (
            (
                await session.execute(
                    select(cpg_nodes.c.symbol_name).where(cpg_nodes.c.snapshot_id == snapshot_id)
                )
            )
            .scalars()
            .all()
        )
        assert "execute_user_command" in symbols
        assert "login" in symbols
        assert "get_db" in symbols
        evidence_count = await session.scalar(
            select(func.count())
            .select_from(evidence_records)
            .where(evidence_records.c.snapshot_id == snapshot_id)
        )
        assert evidence_count > 0

    # =========================================================================
    # Phase 4: Security Scanning
    # =========================================================================
    async with broker_factory() as session:
        located = await locate_source(session, ctx.org_id, repo_id, snapshot_id)

    auth_file = next(f for f in located.files if f.path == "app/auth.py")
    scan_profile = ScannerProfileV1(
        tool="semgrep",
        artifact_sha256="c" * 64,
        rules_sha256="d" * 64,
        rule_ids=("python.shell-true",),
    )
    scan_catalog = reviewed_catalog((scan_profile,))
    scan_config = ScanConfigV1(
        runtime_digest="a" * 64,
        catalog_digest=scan_catalog.semantic_hash(),
        native_selections=(
            NativeSelectionV1(
                profile=scan_profile,
                files=(
                    ScannerFileV1(
                        file_id=auth_file.file_id,
                        sha256=auth_file.sha256,
                        size_bytes=auth_file.size_bytes,
                    ),
                ),
            ),
        ),
    )

    class JourneyScanCapabilities(Registry):
        def catalog(self, digest):
            assert scan_catalog.semantic_hash() == digest
            return scan_catalog

        def native(self, request, located_source):
            class Native:
                async def scan_attested(self, received):
                    assert received == request
                    auth_bytes = AUTH_PY_CONTENT.encode("utf-8")
                    idx = auth_bytes.index(b"subprocess.run(cmd, shell=True)")
                    span = SourceIndex(auth_bytes).range_from_bytes(
                        idx, idx + len(b"subprocess.run(cmd, shell=True)")
                    )
                    result = ScannerResultV1(
                        request_digest=request.semantic_hash(),
                        status="completed",
                        processed_file_ids=(auth_file.file_id,),
                        omitted_file_ids=(),
                        observations=(
                            ScannerObservationV1(
                                rule_id="python.shell-true",
                                file_id=auth_file.file_id,
                                sha256=auth_file.sha256,
                                range=span,
                            ),
                        ),
                    )
                    return _issue_scan(
                        request, result, "sha256:" + scan_config.runtime_digest, "1.179.0"
                    )

            return Native()

    scan_capabilities = JourneyScanCapabilities()
    _, scan_job = await ScanJobService(api_factory, scan_capabilities).create(
        ctx, repo_id, snapshot_id, scan_config, "journey-scan"
    )
    scan_lease = await JobService(broker_factory).claim(ctx.org_id, scan_job.id, 1, "scan-worker")
    scan_op = ScanOperation(
        broker_factory, store, scan_capabilities, FindingFinalizer(broker_factory)
    )
    scan_draft = await scan_op.execute(scan_lease)
    assert scan_draft.coverage == "complete"
    assert len(scan_draft.findings) == 1
    detected_finding = scan_draft.findings[0]
    assert detected_finding.rule_id == "python.shell-true"
    assert detected_finding.id is not None

    scan_read = await ScanReadService(api_factory, scan_capabilities).read(ctx, scan_lease.run_ref)
    assert len(scan_read.historical.findings) == 1
    assert scan_read.historical.findings[0].id == detected_finding.id

    # =========================================================================
    # Phase 5: Exact & Dense Retrieval (Indexing & Search with Docker Runtime)
    # =========================================================================
    reader = DockerSourceReader(
        ScannerOwnership.create(tmp_path / "journey-reader", reader_image),
        DockerController(docker_runtime_paths[0], docker_runtime_paths[1], tmp_path / "reader-cfg"),
        store,
    )
    verified_root = tmp_path / "journey-verified"
    verified_root.mkdir(mode=0o700)
    builder = DockerIndexBuilder(
        ScannerOwnership.create(tmp_path / "journey-indexer", indexer_image),
        IndexDockerController(
            docker_runtime_paths[0], docker_runtime_paths[1], tmp_path / "indexer-cfg"
        ),
        verified_root,
        runtime_source_sha256=source_hash,
    )
    index_cfg = IndexBuildConfigV1(runtime_image=indexer_image, runtime_source_sha256=source_hash)
    index_created = await IndexJobService(api_factory, builder).create(
        ctx, repo_id, snapshot_id, index_cfg, "journey-index"
    )
    index_lease = await JobService(broker_factory).claim(
        ctx.org_id, index_created.job.id, 1, "indexer-worker"
    )
    assert isinstance(index_lease, AttemptLeaseV2)

    index_op = IndexOperation(
        broker_factory,
        store,
        reader,
        builder,
        IndexFinalizer(broker_factory),
        verified_root=verified_root,
    )
    index_receipt = await index_op.execute(index_lease)
    assert index_receipt.chunk_set_id is not None
    assert index_receipt.index_id is not None

    search_scope = SearchScopeV1(
        org_id=ctx.org_id,
        principal_id=ctx.principal_id,
        repository_id=repo_id,
        snapshot_id=snapshot_id,
    )

    async with api_factory() as s, s.begin():
        # Exact search verifying byte-level match on app/auth.py
        exact_res = await LexicalSearchService(s).search_exact(
            ExactSearchRequestV1(scope=search_scope, query="auth.py")
        )
        assert len(exact_res.hits) >= 1
        assert any(h.file_path == "app/auth.py" for h in exact_res.hits)

        # Lexical search verifying symbol match
        lexical_res = await LexicalSearchService(s).search_lexical(
            LexicalSearchRequestV1(scope=search_scope, query="execute_user_command")
        )
        assert len(lexical_res.hits) >= 1
        assert any(h.file_path == "app/auth.py" for h in lexical_res.hits)

        # Dense / hybrid search verifying hit on subprocess
        hybrid_res = await HybridSearchService(s).search_hybrid(
            HybridSearchRequestV1(scope=search_scope, query="subprocess")
        )
        assert len(hybrid_res.hits) >= 1
        auth_hit = next(h for h in hybrid_res.hits if h.file_path == "app/auth.py")

        # Context assembly verifying bounded payload
        assembled = await ContextAssemblyService(s).assemble_context(
            ContextAssemblyRequestV1(
                scope=search_scope,
                query="subprocess",
                candidate_hits=(auth_hit,),
                budget=ContextBudgetV1(max_tokens=256, max_bytes=1024, reserved_overhead_tokens=0),
            )
        )
        assert assembled.items_count >= 1
        assert "subprocess.run(cmd, shell=True)" in assembled.items[0].safe_content

        # =====================================================================
        # Phase 5: Reviewed Memory Promotion
        # =====================================================================
        pointer = EvidencePointerV1(
            file_path=auth_hit.file_path,
            start_line=auth_hit.start_line,
            end_line=auth_hit.end_line,
            chunk_id=auth_hit.chunk_id,
            sha256=auth_hit.content_sha256,
            chunk_set_id=auth_hit.chunk_set_id,
            config_hash=auth_hit.config_hash,
            materialization_hash=auth_hit.materialization_hash,
            safe_content_sha256=auth_hit.safe_content_sha256,
        )
        created_mem = await MemoryService(s).record_memory(
            MemoryCreateRequestV1(
                org_id=ctx.org_id,
                repository_id=repo_id,
                snapshot_id=snapshot_id,
                principal_id=ctx.principal_id,
                memory_kind=MemoryKind.ARCHITECTURE_FACT,
                topic="command-execution-risk",
                summary="app/auth.py execute_user_command executes shell commands via subprocess",
                evidence_pointers=(pointer,),
                request_key="journey-mem-create",
            )
        )
        assert created_mem.state == "candidate"

    # Review and promote candidate to accepted under admin role
    async with migration_factory() as s, s.begin():
        await s.execute(
            memberships.update().where(memberships.c.org_id == ctx.org_id).values(role="admin")
        )

    async with api_factory() as s, s.begin():
        reviewed_mem = await MemoryService(s).review_memory(
            ctx.org_id,
            repo_id,
            ctx.principal_id,
            created_mem.memory_id,
            MemoryReviewRequestV1(
                action="accept",
                expected_version=1,
                request_key="journey-mem-review",
                reason="Confirmed shell execution risk from static scan observation",
            ),
        )
        assert reviewed_mem.state == "accepted"

        # Query memory and verify active status
        mem_query = await MemoryService(s).query_memory(
            MemoryQueryRequestV1(
                org_id=ctx.org_id,
                repository_id=repo_id,
                principal_id=ctx.principal_id,
                snapshot_id=snapshot_id,
                query="subprocess",
                topic="command-execution-risk",
            )
        )
        assert len(mem_query.items) == 1
        assert mem_query.items[0].memory_id == created_mem.memory_id
        assert "subprocess" in mem_query.items[0].summary

    # =========================================================================
    # Phase 6: Saved Investigation Execution & Recovery
    # =========================================================================
    inv_claim_id = uuid4()
    inv_calls, inv_receipts = [], []
    profile_obj = gateway.registry.config.profiles[0].model_copy(
        update={"allowed_task_types": ("short_path", "planner", "auditor", "synthesizer")}
    )
    registry = ModelRegistry(
        ModelRegistryConfigV1(
            profiles=(profile_obj,),
            task_routes=tuple(
                ModelTaskRouteV1(task_type=task, aliases=(profile_obj.alias,))
                for task in profile_obj.allowed_task_types
            ),
        ),
        registered_providers=frozenset({profile_obj.provider}),
        active_prices=frozenset(
            {(profile_obj.provider.value, profile_obj.model_id, profile_obj.price_version)}
        ),
    )

    def provider(request):
        body = json.loads(request.content)
        inv_calls.append(body)
        for item in body.get("input", []):
            if item.get("type") == "function_call_output":
                result = json.loads(item["output"])
                inv_receipts[:] = result.get("evidence_refs", [])
        if body.get("tools") and not inv_receipts:
            output = [
                {
                    "type": "function_call",
                    "id": "call-read-auth",
                    "call_id": "read-auth-file",
                    "name": "read_file_range",
                    "arguments": json.dumps(
                        {"file_path": "app/auth.py", "start_line": 5, "end_line": 8}
                    ),
                }
            ]
        else:
            conclusion = {
                "verdict": "true_positive",
                "confidence": 0.95,
                "summary": "Verified remote command execution via shell=True in app/auth.py",
                "claims": [
                    {
                        "claim_id": str(inv_claim_id),
                        "kind": "source_observation",
                        "statement": "Command execution directly invokes shell=True.",
                        "evidence_ids": inv_receipts,
                        "suggested_file": "app/auth.py",
                        "suggested_line_start": 7,
                        "suggested_line_end": 7,
                        "suggested_quote": "subprocess.run(cmd, shell=True)",
                    }
                ],
            }
            output = [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": json.dumps(conclusion)}],
                }
            ]
        return httpx.Response(
            200,
            json={
                "id": "journey-investigation-response",
                "model": profile_obj.model_id,
                "status": "completed",
                "output": output,
                "usage": {
                    "input_tokens": 50,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens": 30,
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(provider))

    async def secret(_):
        return SecretStr("synthetic-only")

    runtime = ConfiguredModelRuntime(
        registry,
        {"openai": HTTPModelTransport(OpenAIResponsesCodec(), client, secret)},
        broker_factory,
        continuations=gateway.continuations,
        admission_factory=api_factory,
        disclosure_policies={(ctx.org_id, repo_id): args["disclosure_policy"]},
        source_reader=reader,
        scan_capabilities=scan_capabilities,
    )
    worker = build_worker_runtime(
        factory=broker_factory,
        queue=MemoryQueue(),
        store=store,
        admission=None,
        model_runtime=runtime,
        propose_report_memory=True,
    )
    inv_operation = worker.consumer.investigation_operation
    inv_commands = InvestigationJobService(api_factory)

    created_inv = await inv_commands.create(
        ctx,
        InvestigationCreateRequestV2(
            repository_id=repo_id,
            snapshot_id=snapshot_id,
            target_type="finding",
            finding_id=detected_finding.id,
            finding_run_id=scan_lease.run_ref.run_id,
            query="Investigate command injection in execute_user_command",
            mode=InvestigationMode.SHORT_PATH,
            depth=InvestigationDepth.STANDARD,
            budget_limit_micros=1000000,
        ),
        request_key="journey-inv-create",
    )
    started_inv = await inv_commands.start(ctx, created_inv.id, request_key="journey-inv-start")
    inv_lease = await JobService(broker_factory).claim(
        ctx.org_id, started_inv.job_id, 1, "inv-worker"
    )
    assert inv_lease is not None

    inv_outcome = await inv_operation.execute(inv_lease)
    assert inv_outcome.status == InvestigationAdvanceStatus.READY_TO_PUBLISH
    assert len(inv_outcome.draft.claims) == 1
    assert inv_outcome.draft.claims[0].claim_id == inv_claim_id
    assert inv_outcome.draft.verifications[0].status.value == "source_correspondence_confirmed"
    assert inv_outcome.draft.verdict == "true_positive"
    assert inv_outcome.draft.verdict_confidence == 0.95

    # Trigger proposal generation for report memory
    proposals_count = await inv_operation.memory_proposals.tick()
    assert proposals_count == 1

    # Verify persistence of investigation results and spend ledger in PostgreSQL
    async with broker_factory() as session:
        # Check report
        report_row = (
            (
                await session.execute(
                    select(investigation_reports).where(
                        investigation_reports.c.investigation_id == created_inv.id
                    )
                )
            )
            .mappings()
            .one()
        )
        assert report_row["verdict"] == "true_positive"
        assert report_row["verdict_confidence"] == 0.95

        # Check evidence receipts recorded from DockerSourceReader
        ev_count = await session.scalar(
            select(func.count())
            .select_from(investigation_evidence)
            .where(investigation_evidence.c.investigation_id == created_inv.id)
        )
        assert ev_count >= 1

        # Check spend ledger settled
        ledger_count = await session.scalar(
            select(func.count())
            .select_from(model_spend_ledger)
            .where(model_spend_ledger.c.investigation_id == created_inv.id)
        )
        assert ledger_count == len(inv_calls)

        # Check checkpoints recorded
        ckpt_count = await session.scalar(
            select(func.count())
            .select_from(investigation_execution_checkpoints)
            .where(investigation_execution_checkpoints.c.investigation_id == created_inv.id)
        )
        assert ckpt_count >= 1

        # Check report memory candidate created in repo_memories
        report_cand_count = await session.scalar(
            select(func.count())
            .select_from(repo_memories)
            .where(
                (repo_memories.c.repository_id == repo_id) & (repo_memories.c.state == "candidate")
            )
        )
        assert report_cand_count >= 1

    # =========================================================================
    # Verify Saved Investigation Recovery without Re-Execution
    # =========================================================================
    from app.services.investigation_queries import InvestigationQueryService

    detail = await InvestigationQueryService(api_factory).detail(ctx, repo_id, created_inv.id)
    assert detail.summary.status in ("completed", "succeeded")
    assert detail.summary.finding_id == detected_finding.id
    assert detail.report is not None
    assert detail.report["verdict"] == "true_positive"
    assert detail.claims_count == 1
    assert detail.evidence_count >= 1

    # Clean up Docker resources and HTTP clients
    await reader.recover_owned()
    await client.aclose()
