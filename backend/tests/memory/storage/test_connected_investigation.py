"""Real admitted source and encrypted recovery, with synthetic provider exchanges."""

import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from app.adapters.model.http_transport import HTTPModelTransport
from app.adapters.model.openai import OpenAIResponsesCodec
from app.adapters.queue.memory import MemoryQueue
from app.adapters.storage.local import LocalObjectStore
from app.contracts.investigation import InvestigationMode
from app.contracts.investigation_execution import InvestigationAdvanceStatus
from app.contracts.investigation_jobs import InvestigationCreateRequestV2
from app.contracts.model_configuration import ModelRegistryConfigV1, ModelTaskRouteV1
from app.models.investigation_execution_tables import investigation_reports
from app.models.memory_tables import repo_memories
from app.models.model_gateway_tables import model_spend_ledger
from app.models.phase2_tables import PHASE2_TABLES
from app.models.tables import artifacts, source_snapshots
from app.scanners.docker import DockerController
from app.scanners.ownership import ScannerOwnership
from app.security.authorization import require_repository
from app.services.investigation_jobs import InvestigationJobService
from app.services.jobs import JobService
from app.services.model_registry import ModelRegistry
from app.services.model_runtime import ConfiguredModelRuntime
from app.source_read.adapter import DockerSourceReader
from app.workers.composition import build_worker_runtime
from pydantic import SecretStr
from sqlalchemy import func, select, text
from tests.model_connections.storage.test_configured_gateway import configured


@pytest.mark.parametrize("mode", ["short_path", "two_specialist"])
async def test_source_tools_claims_crash_draft_reload_and_memory_candidate(
    migration_factory,
    api_factory,
    broker_factory,
    tmp_path,
    mode,
):
    image = os.environ.get("CODESPY_SOURCE_READER_IMAGE")
    if image is None:
        pytest.skip("owned isolated source reader image required")
    gateway, args, prior_client, _ = await configured(migration_factory, tmp_path)
    await prior_client.aclose()
    async with migration_factory() as session:
        ctx = await require_repository(
            session, args["org_id"], args["principal_id"], args["repository_id"], write=True
        )
        snapshot = await session.scalar(
            select(source_snapshots.c.id)
            .join(
                artifacts,
                (artifacts.c.org_id == source_snapshots.c.org_id)
                & (artifacts.c.id == source_snapshots.c.artifact_id),
            )
            .join(
                PHASE2_TABLES["object_records"],
                (PHASE2_TABLES["object_records"].c.org_id == artifacts.c.org_id)
                & (PHASE2_TABLES["object_records"].c.key == artifacts.c.storage_key)
                & (PHASE2_TABLES["object_records"].c.state == "available"),
            )
            .where(source_snapshots.c.org_id == ctx.org_id)
        )
    profile = gateway.registry.config.profiles[0].model_copy(
        update={"allowed_task_types": ("short_path", "planner", "auditor", "synthesizer")}
    )
    registry = ModelRegistry(
        ModelRegistryConfigV1(
            profiles=(profile,),
            task_routes=tuple(
                ModelTaskRouteV1(task_type=task, aliases=(profile.alias,))
                for task in profile.allowed_task_types
            ),
        ),
        registered_providers=frozenset({profile.provider}),
        active_prices=frozenset(
            {(profile.provider.value, profile.model_id, profile.price_version)}
        ),
    )
    calls, receipts = [], []
    claim_id = uuid4()

    def provider(request):
        body = json.loads(request.content)
        calls.append(body)
        for item in body["input"]:
            if item.get("type") == "function_call_output":
                result = json.loads(item["output"])
                receipts[:] = result["evidence_refs"]
        if body.get("tools") and not receipts:
            output = [
                {
                    "type": "function_call",
                    "id": "call-item",
                    "call_id": "read-file",
                    "name": "read_file_range",
                    "arguments": json.dumps({"file_path": "a.py", "start_line": 1, "end_line": 1}),
                }
            ]
        else:
            conclusion = {
                "verdict": "indeterminate",
                "confidence": 0.4,
                "summary": "Source observation",
                "claims": [
                    {
                        "claim_id": str(claim_id),
                        "kind": "source_observation",
                        "statement": "The source calls print with one.",
                        "evidence_ids": receipts,
                        "suggested_file": "a.py",
                        "suggested_line_start": 1,
                        "suggested_line_end": 1,
                        "suggested_quote": "print(1)",
                    }
                ]
                if receipts
                else [],
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
                "id": "fixture-response",
                "model": profile.model_id,
                "status": "completed",
                "output": output,
                "usage": {
                    "input_tokens": 10,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens": 5,
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(provider))

    async def secret(_):
        return SecretStr("synthetic-only")

    store = LocalObjectStore(tmp_path / "objects")
    reader = DockerSourceReader(
        ScannerOwnership.create(tmp_path / "reader", image),
        DockerController(
            Path("/usr/local/bin/docker"),
            Path.home() / ".docker/run/docker.sock",
            tmp_path / "docker-config",
        ),
        store,
    )
    runtime = ConfiguredModelRuntime(
        registry,
        {"openai": HTTPModelTransport(OpenAIResponsesCodec(), client, secret)},
        broker_factory,
        continuations=gateway.continuations,
        admission_factory=api_factory,
        disclosure_policies={(ctx.org_id, args["repository_id"]): args["disclosure_policy"]},
        source_reader=reader,
    )
    worker = build_worker_runtime(
        factory=broker_factory,
        queue=MemoryQueue(),
        store=store,
        admission=None,
        model_runtime=runtime,
        propose_report_memory=True,
    )
    operation = worker.consumer.investigation_operation
    commands = InvestigationJobService(api_factory)
    created = await commands.create(
        ctx,
        InvestigationCreateRequestV2(
            repository_id=args["repository_id"],
            snapshot_id=snapshot,
            target_type="repository",
            query="Inspect print call",
            mode=InvestigationMode(mode),
        ),
        request_key="connected-create",
    )
    started = await commands.start(ctx, created.id, request_key="connected-start")
    lease = await JobService(broker_factory).claim(
        ctx.org_id, started.job_id, 1, "connected-worker"
    )
    assert lease is not None
    original_factory = operation.executor_factory
    crashed = False

    def interrupted(lease):
        executor = original_factory(lease)
        append = executor.checkpoint_repo.append

        async def crash_before_checkpoint(session, lease, **kwargs):
            nonlocal crashed
            state = kwargs["state"]
            if state.committed_tool_receipt_ids and not crashed:
                crashed = True
                raise RuntimeError("fixture crash after committed tool")
            return await append(session, lease, **kwargs)

        executor.checkpoint_repo.append = crash_before_checkpoint
        return executor

    operation.executor_factory = interrupted
    try:
        try:
            unexpected = await operation.execute(lease)
        except RuntimeError as error:
            assert "fixture crash" in str(error)
        else:
            raise AssertionError(
                (unexpected.status, unexpected.stop_reason, unexpected.message, len(calls))
            )
        assert crashed
        # A new executor/process uses the same encrypted effects, not the previous object.
        operation.executor_factory = original_factory
        publish = operation.finalizer.publish

        async def crash_before_publication(*args, **kwargs):
            raise RuntimeError("fixture crash after draft checkpoint")

        operation.finalizer.publish = crash_before_publication
        with pytest.raises(RuntimeError, match="draft checkpoint"):
            await operation.execute(lease)
        calls_before_resume = len(calls)
        operation.finalizer.publish = publish
        outcome = await operation.execute(lease)
        assert outcome.status == InvestigationAdvanceStatus.READY_TO_PUBLISH
        assert len(calls) == calls_before_resume
        assert outcome.draft.claims[0].claim_id == claim_id
        assert outcome.draft.verifications[0].status.value == "source_correspondence_confirmed"
        assert await operation.memory_proposals.tick() == 1
        assert await operation.memory_proposals.tick() == 0
        async with broker_factory() as session:
            assert (
                await session.scalar(select(func.count()).select_from(investigation_reports)) == 1
            )
            assert await session.scalar(
                select(func.count()).select_from(model_spend_ledger)
            ) == len(calls)
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(repo_memories)
                    .where(repo_memories.c.state == "candidate")
                )
                == 1
            )
            state = await session.scalar(
                text(
                    "SELECT state_snapshot_json FROM investigation_execution_checkpoints "
                    "ORDER BY checkpoint_sequence DESC LIMIT 1"
                )
            )
            assert "print(1)" not in json.dumps(state)
    finally:
        await reader.recover_owned()
        await client.aclose()
