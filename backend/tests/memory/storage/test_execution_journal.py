"""Restricted encrypted effects retain exact scope, replay and database expiry."""

from uuid import uuid4

import pytest
from app.contracts.investigation_execution import InvestigationScopeV1
from app.contracts.investigation_jobs import InvestigationCreateRequestV2
from app.domain.errors import InvalidTransition
from app.models.investigation_effect_tables import investigation_effects
from app.repositories.investigation_execution_repository import SqlInvestigationSpecRepository
from app.services.investigation_jobs import InvestigationJobService
from app.services.investigation_journal import SqlExecutionJournal
from app.services.jobs import JobService
from app.services.model_continuations import ContinuationCipher
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from tests.retrieval.storage.test_index_guards import source_case


async def test_effect_scope_replay_role_denial_and_real_expiry(
    migration_factory, api_factory, broker_factory, tmp_path
):
    ctx, source, _ = await source_case(migration_factory, tmp_path)
    commands = InvestigationJobService(migration_factory)
    created = await commands.create(
        ctx,
        InvestigationCreateRequestV2(
            repository_id=source.repository_id,
            snapshot_id=source.snapshot_id,
            query="Inspect effects",
            target_type="repository",
        ),
        request_key="journal-create",
    )
    started = await commands.start(ctx, created.id, request_key="journal-start")
    lease = await JobService(broker_factory).claim(ctx.org_id, started.job_id, 1, "journal-worker")
    assert lease is not None
    async with broker_factory() as session:
        spec = await SqlInvestigationSpecRepository().get_spec(
            session, ctx.org_id, created.spec_hash
        )
    scope = InvestigationScopeV1(
        org_id=ctx.org_id,
        repository_id=source.repository_id,
        snapshot_id=source.snapshot_id,
        principal_id=ctx.principal_id,
        investigation_id=created.id,
        spec_hash=created.spec_hash,
        execution_revision=spec.runtime_revision,
    )
    journal = SqlExecutionJournal(broker_factory, ContinuationCipher(bytes(range(32))))
    identity = uuid4()
    payload = {"content": "fixture-only source text"}
    await journal.put(scope, lease, identity, "model", "a" * 64, payload)
    await journal.put(scope, lease, identity, "model", "a" * 64, payload)
    assert (await journal.get(scope, identity))["payload"] == payload
    for changes in (
        {"principal_id": uuid4()},
        {"snapshot_id": uuid4()},
        {"repository_id": uuid4()},
    ):
        assert await journal.get(scope.model_copy(update=changes), identity) is None
    with pytest.raises(InvalidTransition):
        await journal.put(scope, lease, identity, "model", "b" * 64, payload)
    with pytest.raises(InvalidTransition):
        await journal.put(
            scope, lease.model_copy(update={"owner": "other"}), uuid4(), "tool", "a" * 64, {}
        )
    for factory in (api_factory, broker_factory):
        async with factory() as session, session.begin():
            with pytest.raises(DBAPIError):
                await session.execute(select(investigation_effects.c.ciphertext))
    async with migration_factory() as session, session.begin():
        cipher = await session.scalar(select(investigation_effects.c.ciphertext))
        assert b"fixture-only" not in cipher
        await session.execute(
            text(
                "UPDATE investigation_effects SET expires_at=clock_timestamp()-interval '1 second'"
            )
        )
    assert await journal.get(scope, identity) is None
    async with broker_factory() as session, session.begin():
        assert await session.scalar(text("SELECT codespy_expire_investigation_effects(100)")) == 1
        assert await session.scalar(text("SELECT codespy_expire_investigation_effects(100)")) == 0
    async with migration_factory() as session:
        row = (
            await session.execute(
                text("SELECT ciphertext,payload_digest FROM investigation_effects")
            )
        ).one()
        assert row.ciphertext is None and len(row.payload_digest) == 64


async def test_request_overflow_stops_before_reservation_or_provider(migration_factory, tmp_path):
    from app.contracts.context_manifest import RequestContextLimitsV1
    from app.contracts.model import ChatMessageV1, ModelRole
    from app.models.model_gateway_tables import model_reservations
    from app.services.configured_model_gateway import ConfiguredModelGateway
    from app.services.context_composition import ContextBudgetExceededError
    from tests.model_connections.storage.test_configured_gateway import configured

    original, args, client, calls = await configured(migration_factory, tmp_path)
    gateway = ConfiguredModelGateway(
        migration_factory,
        original.registry,
        original.transports,
        continuations=original.continuations,
        context_limits=RequestContextLimitsV1(max_input_tokens=128, max_wire_bytes=512),
    )
    args["messages"] = (ChatMessageV1(role=ModelRole.USER, content="input " * 1000),)
    try:
        with pytest.raises(ContextBudgetExceededError):
            await gateway.execute_call(**args)
        assert calls == []
        async with migration_factory() as session:
            assert await session.scalar(select(func.count()).select_from(model_reservations)) == 0
    finally:
        await client.aclose()
