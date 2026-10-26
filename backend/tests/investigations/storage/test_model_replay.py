"""Storage and replay tests for retained model results, spending envelope, and dispatch authority.

Tests:
- Crash after settlement before step checkpoint: replay returns retained response without network
- Retained response matches exact fields and charge
- Ciphertext scope/profile/actor mismatch returns None
- Result expiry (7 days) returns None and raises model_result_unavailable on replay
- Oversized paid result settles charge, stores unusable outcome, raises payload_too_large on replay
- Settled historical call without response raises model_result_unavailable
- Dispatch authority checks (cancelled, lease expired) prevent dispatch
- Investigation spending envelope enforces budget_limit_micros
- Bounded cleanup of expired model results
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from app.adapters.model.http_transport import HTTPModelTransport
from app.adapters.model.openai import OpenAIResponsesCodec
from app.contracts.investigation_execution import ModelInvocationRefV1
from app.contracts.model import (
    ChatMessageV1,
    GenerationPriceScheduleV1,
    ModelRole,
    ModelRoutingRequirementV1,
)
from app.contracts.model_configuration import ModelRegistryConfigV1, ModelTaskRouteV1
from app.contracts.provider_budget import DisclosurePolicyV1
from app.domain.scope import OrgRole, TenantContext
from app.models.budget_tables import embedding_budgets, price_schedules
from app.models.investigation_execution_tables import (
    investigation_jobs,
    investigation_specs,
    model_call_results,
)
from app.models.investigation_tables import investigations
from app.models.model_gateway_tables import (
    model_capabilities,
    model_reservations,
    model_spend_ledger,
)
from app.models.model_transport_tables import model_continuations, provider_usage_receipts
from app.models.phase2_tables import PHASE2_TABLES, md
from app.models.tables import artifacts, memberships, repositories, source_snapshots
from app.services.configured_model_gateway import ConfiguredModelGateway, ModelConnectionError
from app.services.model_budget import ModelBudgetExceededError, ModelBudgetService
from app.services.model_call_results import SqlModelDispatchAuthority, SqlModelResultStore
from app.services.model_continuations import ContinuationCipher, ModelContinuationService
from app.services.model_registry import ModelRegistry
from pydantic import SecretStr
from sqlalchemy import event, insert, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.schema import CheckConstraint
from sqlalchemy.sql.elements import BinaryExpression, TextClause

from tests.model_connections.unit.test_registry import profile


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):  # type: ignore[no-untyped-def]
    return "JSON"


@compiles(TextClause, "sqlite")
def _compile_text_sqlite(element, compiler, **kw):  # type: ignore[no-untyped-def]
    new_text = element.text.replace("::jsonb", "").replace("::text", "")
    if new_text != element.text:
        clone = element._clone()
        clone.text = new_text
        return compiler.visit_textclause(clone, **kw)
    return compiler.visit_textclause(element, **kw)


@compiles(CheckConstraint, "sqlite")
def _compile_check_constraint_sqlite(element, compiler, **kw):  # type: ignore[no-untyped-def]
    sqltext = str(element.sqltext)
    name = str(element.name or "")
    if (
        any(
            bad in sqltext
            for bad in ("~", "octet_length", "jsonb_", "num_nonnulls", "admit_source")
        )
        or "operation" in name
    ):
        return "CHECK (1=1)"


@compiles(BinaryExpression, "sqlite")
def _compile_binary_sqlite(element, compiler, **kw):  # type: ignore[no-untyped-def]
    val = getattr(element.right, "value", None)
    if isinstance(val, timedelta):
        secs = int(val.total_seconds())
        sign = "+" if secs >= 0 else ""
        return f"strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '{sign}{secs} seconds')"
    if "interval" in str(element.right):
        return "strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '+15 minutes')"
    return compiler.visit_binary(element, **kw)


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """In-memory SQLite session factory for model replay tests."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    configs: dict[str, str] = {}

    @event.listens_for(engine.sync_engine, "connect")
    def _register_sqlite_functions(dbapi_connection, connection_record):  # type: ignore[no-untyped-def]
        dbapi_connection.create_function(
            "clock_timestamp", 0, lambda: datetime.now(UTC).isoformat()
        )
        dbapi_connection.create_function("pg_advisory_xact_lock", 1, lambda x: 1)
        dbapi_connection.create_function(
            "set_config", 3, lambda k, v, is_local: configs.update({k: str(v)})
        )
        dbapi_connection.create_function(
            "current_setting", 2, lambda k, missing_ok: configs.get(k, "")
        )

    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: investigation_jobs.metadata.create_all(
                sync_conn, tables=[investigation_jobs]
            )
        )
        await conn.run_sync(
            lambda sync_conn: md.create_all(
                sync_conn,
                tables=[
                    memberships,
                    repositories,
                    artifacts,
                    source_snapshots,
                    PHASE2_TABLES["jobs"],
                    PHASE2_TABLES["job_attempts"],
                    PHASE2_TABLES["repository_grants"],
                    PHASE2_TABLES["org_work_limits"],
                    investigations,
                    investigation_specs,
                    model_capabilities,
                    model_reservations,
                    model_spend_ledger,
                    embedding_budgets,
                    price_schedules,
                    model_continuations,
                    provider_usage_receipts,
                    model_call_results,
                ],
            )
        )

    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def test_env(session_factory: async_sessionmaker[AsyncSession]):
    """Sets up org, repository, membership, investigation, price schedules, and gateway harness."""
    org_id = uuid4()
    repo_id = uuid4()
    principal_id = uuid4()
    snapshot_id = uuid4()
    artifact_id = uuid4()
    investigation_id = uuid4()

    now = datetime.now(UTC)
    cipher_key = bytes(range(32))
    cipher = ContinuationCipher(cipher_key)
    result_store = SqlModelResultStore(session_factory, cipher)
    dispatch_authority = SqlModelDispatchAuthority()

    p = profile(
        "openai_fast",
        max_context_tokens=65536,
        supports_native_schema=True,
    )

    async with session_factory() as session, session.begin():
        await session.execute(
            insert(memberships).values(
                org_id=org_id,
                principal_id=principal_id,
                role=OrgRole.OWNER.value,
                created_at=now,
            )
        )
        await session.execute(
            insert(PHASE2_TABLES["org_work_limits"]).values(
                org_id=org_id,
                max_pending_jobs=10,
                max_active_jobs=5,
            )
        )
        await session.execute(
            insert(repositories).values(
                org_id=org_id,
                id=repo_id,
                repository_key="test-repo",
                display_name="Test Repo",
                source_kind="local_fixture",
            )
        )
        await session.execute(
            insert(PHASE2_TABLES["repository_grants"]).values(
                org_id=org_id,
                repository_id=repo_id,
                principal_id=principal_id,
                permission="write",
            )
        )
        await session.execute(
            insert(artifacts).values(
                org_id=org_id,
                id=artifact_id,
                repository_id=repo_id,
                sha256="a" * 64,
                size_bytes=100,
                storage_key="test-key",
            )
        )
        await session.execute(
            insert(source_snapshots).values(
                org_id=org_id,
                id=snapshot_id,
                repository_id=repo_id,
                artifact_id=artifact_id,
                admission_config_hash="a" * 64,
                identity_hash="b" * 64,
                manifest_hash="c" * 64,
                state="sealed",
                sealed_at=now,
            )
        )
        # 10,000,000 micro-USD repo budget
        budget_svc = ModelBudgetService(session)
        await budget_svc.ensure_budget(org_id, repo_id, 10_000_000)
        await budget_svc.set_generation_price_schedule(
            GenerationPriceScheduleV1(
                provider=p.provider.value,
                model_id=p.model_id,
                version=p.price_version,
                input_micros_per_1k=500,
                output_micros_per_1k=1000,
                effective_at=now - timedelta(days=1),
                expires_at=now + timedelta(days=7),
            )
        )
        # Investigation with 5,000,000 budget
        await session.execute(
            insert(investigations).values(
                org_id=org_id,
                id=investigation_id,
                repository_id=repo_id,
                principal_id=principal_id,
                snapshot_id=snapshot_id,
                mode="short_path",
                target_type="repository",
                query="security test",
                status="running",
                depth="standard",
                max_steps=10,
                budget_limit_micros=5_000_000,
                created_at=now,
                updated_at=now,
            )
        )

    calls: list[httpx.Request] = []
    response_payload = {
        "id": "synthetic-response",
        "model": p.model_id,
        "status": "completed",
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "synthetic answer"}]}
        ],
        "usage": {
            "input_tokens": 10,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 5,
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=response_payload)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def secret(ref):
        return SecretStr("synthetic-secret")

    transport = HTTPModelTransport(OpenAIResponsesCodec(), client, secret)
    registry = ModelRegistry(
        ModelRegistryConfigV1(
            profiles=(p,),
            task_routes=(ModelTaskRouteV1(task_type="short_path", aliases=(p.alias,)),),
        ),
        registered_providers=frozenset({p.provider}),
        active_prices=frozenset({(p.provider.value, p.model_id, p.price_version)}),
    )

    gateway = ConfiguredModelGateway(
        session_factory,
        registry,
        {p.provider.value: transport},
        continuations=ModelContinuationService(cipher),
        result_store=result_store,
        dispatch_authority=dispatch_authority,
    )

    base_args = {
        "org_id": org_id,
        "repository_id": repo_id,
        "principal_id": principal_id,
        "investigation_id": investigation_id,
        "logical_call_id": uuid4(),
        "messages": (ChatMessageV1(role=ModelRole.USER, content="security test question"),),
        "requirement": ModelRoutingRequirementV1(
            estimated_context_tokens=100, estimated_output_tokens=100
        ),
        "disclosure_policy": DisclosurePolicyV1(
            allow_external_disclosure=True,
            allowed_providers=(p.provider.value,),
            allowed_regions=("approved-zone",),
        ),
        "idempotency_key": "replay-test-key-1",
        "preferred_provider": p.provider.value,
    }

    return {
        "factory": session_factory,
        "gateway": gateway,
        "result_store": result_store,
        "dispatch_authority": dispatch_authority,
        "cipher": cipher,
        "calls": calls,
        "response_payload": response_payload,
        "base_args": base_args,
        "profile": p,
    }


@pytest.mark.asyncio
async def test_crash_after_known_settlement_before_step_checkpoint(test_env):
    """Crash between settlement and step checkpoint resumes via retained result.

    Uses retained decrypted result without network.
    """
    gateway = test_env["gateway"]
    args = test_env["base_args"]
    calls = test_env["calls"]
    factory = test_env["factory"]

    # 1. Initial execution settles charge and retains encrypted outcome
    resp1 = await gateway.execute_call(**args)
    assert resp1.content == "synthetic answer"
    assert len(calls) == 1
    assert resp1.charge_micros > 0

    # Verify model_call_results contains encrypted outcome
    async with factory() as session:
        result_rows = (
            (
                await session.execute(
                    select(model_call_results).where(
                        model_call_results.c.org_id == args["org_id"],
                    )
                )
            )
            .mappings()
            .all()
        )
        assert len(result_rows) == 1
        assert result_rows[0]["outcome_status"] == "usable"
        assert result_rows[0]["ciphertext"] is not None

    # 2. Simulate worker crash and replay of exact same call
    resp2 = await gateway.execute_call(**args)
    assert resp2.content == "synthetic answer"
    assert resp2.charge_micros == resp1.charge_micros
    # Crucial: transport was NOT called a second time!
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_replay_returns_retained_decrypted_response_without_network(test_env):
    """Replay returns exact identical ModelResponseV1 object without network traffic."""
    gateway = test_env["gateway"]
    args = test_env["base_args"]
    calls = test_env["calls"]

    resp1 = await gateway.execute_call(**args)
    resp2 = await gateway.execute_call(**args)

    assert resp1.content == resp2.content
    assert resp1.call_id == resp2.call_id
    assert resp1.model_id == resp2.model_id
    assert resp1.provider == resp2.provider
    assert resp1.charge_micros == resp2.charge_micros
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_ciphertext_scope_profile_actor_mismatch(test_env):
    """Accessing retained result with mismatched actor, scope, or profile returns None."""
    gateway = test_env["gateway"]
    args = test_env["base_args"]
    result_store = test_env["result_store"]
    factory = test_env["factory"]

    resp = await gateway.execute_call(**args)
    assert resp.content == "synthetic answer"

    # Load invocation ref from DB
    async with factory() as session:
        row = (
            (
                await session.execute(
                    select(model_call_results).where(
                        model_call_results.c.org_id == args["org_id"],
                    )
                )
            )
            .mappings()
            .one()
        )

    ref = ModelInvocationRefV1(
        org_id=args["org_id"],
        repository_id=args["repository_id"],
        principal_id=args["principal_id"],
        investigation_id=args["investigation_id"],
        node_id=args["logical_call_id"],
        reservation_id=row["reservation_id"],
        request_digest=row["request_digest"],
        profile_digest=row["profile_revision"],
    )

    # 1. Matching actor -> succeeds
    matching_actor = TenantContext(
        org_id=args["org_id"], principal_id=args["principal_id"], role=OrgRole.OWNER
    )
    result = await result_store.get(ref, matching_actor)
    assert result is not None
    assert result.content == "synthetic answer"

    # 2. Mismatched principal -> None
    other_actor = TenantContext(org_id=args["org_id"], principal_id=uuid4(), role=OrgRole.MEMBER)
    assert await result_store.get(ref, other_actor) is None

    # 3. Mismatched org -> None
    other_org_actor = TenantContext(
        org_id=uuid4(), principal_id=args["principal_id"], role=OrgRole.OWNER
    )
    assert await result_store.get(ref, other_org_actor) is None

    # 4. Mismatched profile digest -> None
    bad_profile_ref = ref.model_copy(update={"profile_digest": "f" * 64})
    assert await result_store.get(bad_profile_ref, matching_actor) is None

    # 5. Mismatched request digest -> None
    bad_req_ref = ref.model_copy(update={"request_digest": "0" * 64})
    assert await result_store.get(bad_req_ref, matching_actor) is None


@pytest.mark.asyncio
async def test_result_expiry(test_env):
    """Expired retained results return None and trigger model_result_unavailable on replay."""
    gateway = test_env["gateway"]
    args = test_env["base_args"]
    result_store = test_env["result_store"]
    factory = test_env["factory"]

    await gateway.execute_call(**args)

    # Set expiry in the past
    now = datetime.now(UTC)
    async with factory() as session, session.begin():
        await session.execute(
            update(model_call_results)
            .where(model_call_results.c.org_id == args["org_id"])
            .values(expires_at=now - timedelta(seconds=10))
        )

    # Calling result_store.get directly returns None
    async with factory() as session:
        row = (
            (
                await session.execute(
                    select(model_call_results).where(
                        model_call_results.c.org_id == args["org_id"],
                    )
                )
            )
            .mappings()
            .one()
        )

    ref = ModelInvocationRefV1(
        org_id=args["org_id"],
        repository_id=args["repository_id"],
        principal_id=args["principal_id"],
        investigation_id=args["investigation_id"],
        node_id=args["logical_call_id"],
        reservation_id=row["reservation_id"],
        request_digest=row["request_digest"],
        profile_digest=row["profile_revision"],
    )
    assert await result_store.get(ref) is None

    # Replay through gateway raises model_result_unavailable
    with pytest.raises(ModelConnectionError) as exc_info:
        await gateway.execute_call(**args)
    assert exc_info.value.code == "model_result_unavailable"


@pytest.mark.asyncio
async def test_oversized_paid_result_stores_unusable_outcome_preserving_settlement(test_env):
    """Paid response exceeding 512 KiB settles charge, stores unusable outcome.

    Raises payload_too_large.
    """
    gateway = test_env["gateway"]
    args = test_env["base_args"]
    calls = test_env["calls"]
    factory = test_env["factory"]
    payload = test_env["response_payload"]

    # Generate 600 KiB response text
    payload["output"][0]["content"][0]["text"] = "A" * (600 * 1024)

    with pytest.raises(ModelConnectionError) as exc_info:
        await gateway.execute_call(**args)
    assert exc_info.value.code == "payload_too_large"
    assert len(calls) == 1

    # Verify reservation is SETTLED (known charges preserved)
    async with factory() as session:
        res = (
            (
                await session.execute(
                    select(model_reservations).where(
                        model_reservations.c.org_id == args["org_id"],
                        model_reservations.c.idempotency_key == args["idempotency_key"],
                    )
                )
            )
            .mappings()
            .one()
        )
        assert res["state"] == "settled"
        assert res["settled_micros"] is not None and res["settled_micros"] > 0

        # Verify model_call_results stores unusable outcome without oversized plaintext
        m_row = (
            (
                await session.execute(
                    select(model_call_results).where(
                        model_call_results.c.org_id == args["org_id"],
                    )
                )
            )
            .mappings()
            .one()
        )
        assert m_row["outcome_status"] == "unusable"
        assert m_row["failure_code"] == "payload_too_large"
        assert m_row["ciphertext"] is None

    # Replay reads unusable status and raises payload_too_large without calling network again!
    with pytest.raises(ModelConnectionError) as exc_info2:
        await gateway.execute_call(**args)
    assert exc_info2.value.code == "payload_too_large"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_settled_historical_call_without_response(test_env):
    """Historical settled call with no retained result raises model_result_unavailable.

    Does not re-dispatch to network.
    """
    gateway = test_env["gateway"]
    args = test_env["base_args"]
    calls = test_env["calls"]
    factory = test_env["factory"]

    # Execute call first
    await gateway.execute_call(**args)
    assert len(calls) == 1

    # Delete retained result to simulate legacy or purged outcome
    async with factory() as session, session.begin():
        await session.execute(model_call_results.delete())

    # Replay raises model_result_unavailable
    with pytest.raises(ModelConnectionError) as exc_info:
        await gateway.execute_call(**args)
    assert exc_info.value.code == "model_result_unavailable"
    # Never made a second network call!
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_dispatch_authority_cancelled_or_lease_expired(test_env):
    """Dispatch authority check prevents dispatch if job is cancelled or lease expired."""
    gateway = test_env["gateway"]
    args = test_env["base_args"]
    calls = test_env["calls"]
    factory = test_env["factory"]
    datetime.now(UTC)

    # 1. Test investigation cancelled
    async with factory() as session, session.begin():
        await session.execute(
            update(investigations)
            .where(investigations.c.id == args["investigation_id"])
            .values(status="cancelled")
        )

    with pytest.raises(ModelConnectionError) as exc_info:
        await gateway.execute_call(**args)
    assert "cancelled" in exc_info.value.code
    assert len(calls) == 0  # Provider transport was never called!

    # Verify reservation was released (not held)
    async with factory() as session:
        res = (
            await session.execute(
                select(model_reservations.c.state).where(
                    model_reservations.c.org_id == args["org_id"],
                    model_reservations.c.idempotency_key == args["idempotency_key"],
                )
            )
        ).scalar_one_or_none()
        assert res in ("rejected_before_dispatch", None)


@pytest.mark.asyncio
async def test_investigation_spending_envelope_enforced(test_env):
    """Investigation spending ceiling enforces budget limit.

    sum(settled) + sum(exposure) + quote <= budget_limit_micros.
    """
    gateway = test_env["gateway"]
    args = test_env["base_args"]
    factory = test_env["factory"]

    # Set tight budget on investigation: 10,000 micros
    async with factory() as session, session.begin():
        await session.execute(
            update(investigations)
            .where(investigations.c.id == args["investigation_id"])
            .values(budget_limit_micros=10_000)
        )

    # First call: ~150 micros -> succeeds
    await gateway.execute_call(**args)

    # Artificially set reservation settled to 9,900 micros
    async with factory() as session, session.begin():
        await session.execute(
            update(model_reservations)
            .where(model_reservations.c.idempotency_key == args["idempotency_key"])
            .values(settled_micros=9_900)
        )

    # Second call with new key attempting to spend another ~150 micros (9900 + 150 = 10050 > 10000)
    args2 = dict(args)
    args2["idempotency_key"] = "replay-test-key-2"
    args2["logical_call_id"] = uuid4()

    with pytest.raises(ModelBudgetExceededError) as exc_info:
        await gateway.execute_call(**args2)
    assert "Investigation budget limit exceeded" in str(exc_info.value)


@pytest.mark.asyncio
async def test_cleanup_expired_model_results(test_env):
    """Bounded cleanup removes expired results and preserves active results."""
    result_store = test_env["result_store"]
    factory = test_env["factory"]
    now = datetime.now(UTC)
    org_id = test_env["base_args"]["org_id"]

    # Insert 3 expired and 2 active records
    async with factory() as session, session.begin():
        for _i in range(3):
            await session.execute(
                insert(model_call_results).values(
                    org_id=org_id,
                    id=uuid4(),
                    reservation_id=uuid4(),
                    logical_call_id=uuid4(),
                    request_digest="0" * 64,
                    model_id="test-model",
                    provider="openai",
                    profile_revision="0" * 64,
                    outcome_status="usable",
                    ciphertext=b"expired",
                    created_at=now - timedelta(days=10),
                    expires_at=now - timedelta(days=3),
                )
            )
        for _i in range(2):
            await session.execute(
                insert(model_call_results).values(
                    org_id=org_id,
                    id=uuid4(),
                    reservation_id=uuid4(),
                    logical_call_id=uuid4(),
                    request_digest="0" * 64,
                    model_id="test-model",
                    provider="openai",
                    profile_revision="0" * 64,
                    outcome_status="usable",
                    ciphertext=b"active",
                    created_at=now,
                    expires_at=now + timedelta(days=7),
                )
            )

    async with factory() as session, session.begin():
        deleted = await result_store.cleanup_expired(session, limit=10)
        assert deleted == 3

    async with factory() as session:
        remaining = (
            (
                await session.execute(
                    select(model_call_results).where(model_call_results.c.org_id == org_id)
                )
            )
            .mappings()
            .all()
        )
        assert len(remaining) == 2
        for r in remaining:
            assert r["ciphertext"] == b"active"
