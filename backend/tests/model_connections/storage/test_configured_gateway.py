"""Real PostgreSQL accounting around synthetic provider-shaped exchanges."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from app.adapters.model.http_transport import HTTPModelTransport
from app.adapters.model.openai import OpenAIResponsesCodec
from app.contracts.model import GenerationPriceScheduleV1
from app.contracts.model_configuration import ModelRegistryConfigV1, ModelTaskRouteV1
from app.contracts.provider_budget import DisclosurePolicyV1
from app.models.model_gateway_tables import model_reservations, model_spend_ledger
from app.models.model_transport_tables import provider_usage_receipts
from app.services.configured_model_gateway import ConfiguredModelGateway, ModelConnectionError
from app.services.model_budget import ModelBudgetService
from app.services.model_continuations import ContinuationCipher, ModelContinuationService
from app.services.model_registry import ModelRegistry
from pydantic import SecretStr
from sqlalchemy import func, select, text

from tests.accounting.storage.test_durable_model_gateway import prepared
from tests.model_connections.unit.test_registry import profile


async def configured(factory, tmp_path, payload=None, alias="openai_fast"):
    args = await prepared(factory, tmp_path)
    p = profile(
        alias,
        max_context_tokens=65536,
        supports_native_schema=not alias.startswith("claude"),
        api_version="v1beta" if alias == "gemini_general" else None,
    )
    async with factory() as s, s.begin():
        now = datetime.now(UTC)
        await ModelBudgetService(s).set_generation_price_schedule(
            GenerationPriceScheduleV1(
                provider=p.provider.value,
                model_id=p.model_id,
                version=p.price_version,
                input_micros_per_1k=500,
                output_micros_per_1k=1000,
                effective_at=now - timedelta(days=1),
                expires_at=now + timedelta(days=1),
            )
        )
    calls = []
    if payload is None:
        if alias.startswith("claude"):
            payload = {
                "output": {"message": {"content": [{"text": "answer"}]}},
                "stopReason": "end_turn",
                "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
            }
        elif alias == "gemini_general":
            payload = {
                "candidates": [
                    {"content": {"parts": [{"text": "answer"}]}, "finishReason": "STOP"}
                ],
                "usageMetadata": {
                    "promptTokenCount": 10,
                    "candidatesTokenCount": 5,
                    "thoughtsTokenCount": 0,
                },
            }
        else:
            payload = {
                "id": "synthetic-response",
                "model": p.model_id,
                "status": "completed",
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": "answer"}]}
                ],
                "usage": {
                    "input_tokens": 10,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens": 5,
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            }

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=payload)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def secret(reference):
        return SecretStr("synthetic-only")

    from app.adapters.model.bedrock import BedrockModelTransport
    from app.adapters.model.gemini import GeminiContentCodec

    class LocalConverse:
        retry_total_attempts = 1

        async def converse(self, **kwargs):
            calls.append(kwargs)
            return payload

    transport = (
        BedrockModelTransport(LocalConverse())
        if alias.startswith("claude")
        else HTTPModelTransport(
            GeminiContentCodec() if alias == "gemini_general" else OpenAIResponsesCodec(),
            client,
            secret,
        )
    )
    registry = ModelRegistry(
        ModelRegistryConfigV1(
            profiles=(p,),
            task_routes=(ModelTaskRouteV1(task_type="short_path", aliases=(p.alias,)),),
        ),
        registered_providers=frozenset({p.provider}),
        active_prices=frozenset({(p.provider.value, p.model_id, p.price_version)}),
    )
    gateway = ConfiguredModelGateway(
        factory,
        registry,
        {p.provider.value: transport},
        continuations=ModelContinuationService(ContinuationCipher(bytes(range(32)))),
    )
    args["disclosure_policy"] = DisclosurePolicyV1(
        allow_external_disclosure=True,
        allowed_providers=(p.provider.value,),
        allowed_regions=("approved-zone",),
    )
    return gateway, args, client, calls


async def test_known_usage_receipt_and_replay_do_not_redispatch(migration_factory, tmp_path):
    gateway, args, client, calls = await configured(migration_factory, tmp_path)
    try:
        response = await gateway.execute_call(**args)
        assert response.content == "answer"
        with pytest.raises(ModelConnectionError, match="already_completed"):
            await gateway.execute_call(**args)
        assert len(calls) == 1
        async with migration_factory() as session:
            assert (
                await session.scalar(select(func.count()).select_from(provider_usage_receipts)) == 1
            )
            assert await session.scalar(select(model_reservations.c.state)) == "settled"
            assert await session.scalar(select(func.count()).select_from(model_spend_ledger)) == 1
    finally:
        await client.aclose()


async def test_missing_usage_holds_funds_and_records_unknown(migration_factory, tmp_path):
    gateway, args, client, calls = await configured(migration_factory, tmp_path, payload={})
    try:
        with pytest.raises(ModelConnectionError, match="usage_unknown"):
            await gateway.execute_call(**args)
        with pytest.raises(ModelConnectionError, match="pending_or_terminal"):
            await gateway.execute_call(**args)
        assert len(calls) == 1
        async with migration_factory() as session:
            assert await session.scalar(select(model_reservations.c.state)) == "uncertain_hold"
            assert await session.scalar(select(provider_usage_receipts.c.quality)) == "unknown"
    finally:
        await client.aclose()


async def test_restricted_roles_can_run_without_raw_financial_mutations(
    migration_factory, api_factory, broker_factory, tmp_path
):
    from sqlalchemy.exc import DBAPIError

    gateway, args, client, calls = await configured(migration_factory, tmp_path)
    gateway.factory = broker_factory
    gateway.admission_factory = api_factory
    try:
        result = await gateway.execute_call(**args)
        assert result.content == "answer"
        assert len(calls) == 1
        async with broker_factory() as session:
            for statement in (
                "UPDATE embedding_budgets SET balance_micros=999999",
                "SELECT * FROM model_continuations",
                "SELECT * FROM provider_usage_receipts",
            ):
                with pytest.raises(DBAPIError):
                    await session.execute(text(statement))
                await session.rollback()
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "alias",
    ["openai_fast", "openai_reasoning", "claude_fast", "claude_reasoning", "gemini_general"],
)
async def test_each_configured_slot_runs_through_durable_gateway(
    migration_factory, tmp_path, alias
):
    gateway, args, client, calls = await configured(migration_factory, tmp_path, alias=alias)
    try:
        result = await gateway.execute_call(**args, requested_alias=alias)
        assert result.content == "answer"
        assert len(calls) == 1
        async with migration_factory() as session:
            assert await session.scalar(select(model_reservations.c.state)) == "settled"
            assert (
                await session.scalar(select(func.count()).select_from(provider_usage_receipts)) == 1
            )
    finally:
        await client.aclose()


async def test_concurrent_replay_dispatches_once(migration_factory, tmp_path):
    import asyncio

    gateway, args, client, calls = await configured(migration_factory, tmp_path)
    try:
        results = await asyncio.gather(
            gateway.execute_call(**args), gateway.execute_call(**args), return_exceptions=True
        )
        assert sum(not isinstance(item, BaseException) for item in results) == 1, results
        assert len(calls) == 1
        async with migration_factory() as s:
            assert await s.scalar(select(func.count()).select_from(model_spend_ledger)) == 1
    finally:
        await client.aclose()


async def test_known_usage_is_charged_before_output_validation(migration_factory, tmp_path):
    gateway, args, client, calls = await configured(migration_factory, tmp_path)
    try:
        args["response_schema"] = {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }
        from app.services.output_validator import OutputValidationError

        with pytest.raises(OutputValidationError):
            await gateway.execute_call(**args)
        assert len(calls) == 1
        async with migration_factory() as s:
            assert await s.scalar(select(model_reservations.c.state)) == "settled"
            assert await s.scalar(select(func.count()).select_from(provider_usage_receipts)) == 1
    finally:
        await client.aclose()


async def test_encrypted_tool_state_is_scoped_and_not_plaintext(migration_factory, tmp_path):
    import json
    from uuid import uuid4

    from app.domain.errors import ScopeViolation
    from app.models.model_transport_tables import model_continuations
    from app.security.authorization import require_repository

    payload = {
        "status": "completed",
        "output": [
            {
                "type": "function_call",
                "call_id": "native-id",
                "name": "search_code",
                "arguments": json.dumps({"query": "safe"}),
            }
        ],
        "usage": {
            "input_tokens": 10,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 5,
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }
    gateway, args, client, _calls = await configured(migration_factory, tmp_path, payload)
    from app.contracts.model import ToolDefinitionV1

    args["tools"] = (
        ToolDefinitionV1(
            name="search_code",
            description="Search fixture",
            parameters_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        ),
    )
    try:
        response = await gateway.execute_call(**args)
        assert response.continuation_ref is not None
        digest = gateway.registry.config.profiles[0].model_dump(mode="json")
        from app.domain.canonical_json import semantic_digest

        async with migration_factory() as s:
            actor = await require_repository(
                s, args["org_id"], args["principal_id"], args["repository_id"], write=True
            )
            stored = await gateway.continuations.get(
                s,
                actor,
                args["repository_id"],
                args["investigation_id"],
                response.continuation_ref,
                semantic_digest(digest),
            )
            assert "native-id" in stored
            ciphertext = await s.scalar(select(model_continuations.c.ciphertext))
            assert b"native-id" not in ciphertext
            with pytest.raises(ScopeViolation):
                await gateway.continuations.get(
                    s,
                    actor,
                    args["repository_id"],
                    uuid4(),
                    response.continuation_ref,
                    semantic_digest(digest),
                )
    finally:
        await client.aclose()


async def test_runtime_maintenance_removes_only_expired_protocol_state(migration_factory, tmp_path):
    from uuid import uuid4

    from app.models.model_transport_tables import model_continuations
    from app.services.model_runtime import ConfiguredModelRuntime

    gateway, args, client, _calls = await configured(migration_factory, tmp_path)
    try:
        await gateway.execute_call(**args)
        async with migration_factory() as s, s.begin():
            reservation = await s.scalar(select(model_reservations.c.reservation_id))
            await s.execute(
                model_continuations.insert().values(
                    org_id=args["org_id"],
                    id=uuid4(),
                    repository_id=args["repository_id"],
                    reservation_id=reservation,
                    investigation_id=args["investigation_id"],
                    principal_id=args["principal_id"],
                    profile_digest="a" * 64,
                    ciphertext=b"x" * 28,
                    created_at=text("clock_timestamp()-interval '2 days'"),
                    expires_at=text("clock_timestamp()-interval '1 day'"),
                )
            )
        runtime = ConfiguredModelRuntime(
            gateway.registry, gateway.transports, migration_factory, gateway.continuations
        )
        assert await runtime.maintenance_tick() == 1
        assert await runtime.maintenance_tick() == 0
    finally:
        await client.aclose()


async def test_cancel_after_dispatch_keeps_unknown_usage_and_never_replays(
    migration_factory, tmp_path
):
    import asyncio

    gateway, args, client, calls = await configured(migration_factory, tmp_path)
    entered = asyncio.Event()
    finished = asyncio.Event()

    async def pending(request):
        calls.append(request)
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    replacement = httpx.AsyncClient(transport=httpx.MockTransport(pending))
    transport = gateway.transports["openai"]
    transport.client = replacement
    try:
        task = asyncio.create_task(gateway.execute_call(**args))
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
        async with migration_factory() as s:
            assert await s.scalar(select(model_reservations.c.state)) == "uncertain_hold"
        with pytest.raises(ModelConnectionError):
            await gateway.execute_call(**args)
        assert len(calls) == 1
    finally:
        await replacement.aclose()
        await client.aclose()


async def test_unpriced_reasoning_usage_stays_held(migration_factory, tmp_path):
    payload = {
        "status": "completed",
        "output": [],
        "usage": {
            "input_tokens": 10,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 5,
            "output_tokens_details": {"reasoning_tokens": 2},
        },
    }
    gateway, args, client, _calls = await configured(migration_factory, tmp_path, payload)
    try:
        with pytest.raises(ModelConnectionError, match="usage_unpriced"):
            await gateway.execute_call(**args)
        async with migration_factory() as s:
            assert await s.scalar(select(model_reservations.c.state)) == "uncertain_hold"
    finally:
        await client.aclose()


async def test_unsolicited_tool_state_does_not_undo_known_charge(migration_factory, tmp_path):
    payload = {
        "status": "completed",
        "output": [
            {
                "type": "function_call",
                "call_id": "native-id",
                "name": "search_code",
                "arguments": "{}",
            }
        ],
        "usage": {
            "input_tokens": 10,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 5,
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }
    gateway, args, client, _calls = await configured(migration_factory, tmp_path, payload)
    gateway.continuations = None
    try:
        with pytest.raises(ModelConnectionError):
            await gateway.execute_call(**args)
        async with migration_factory() as s:
            assert await s.scalar(select(model_reservations.c.state)) == "settled"
    finally:
        await client.aclose()
