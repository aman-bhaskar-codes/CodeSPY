"""Configured model discovery and investigation execution through actual API seams."""

from uuid import uuid4

import httpx
import pytest
from app.adapters.storage.local import LocalObjectStore
from app.api.application import ApiSettings, ApplicationPorts, create_app
from app.domain.scope import OrgRole, TenantContext
from app.services.model_runtime import ConfiguredModelRuntime

from tests.model_connections.storage.test_configured_gateway import configured


async def test_model_status_is_scoped_and_contains_no_secret_references(
    migration_factory, tmp_path
):
    gateway, args, client, _calls = await configured(migration_factory, tmp_path)
    runtime = ConfiguredModelRuntime(
        gateway.registry,
        gateway.transports,
        migration_factory,
        gateway.continuations,
        disclosure_policies={(args["org_id"], args["repository_id"]): args["disclosure_policy"]},
    )
    app = create_app(
        ApiSettings(environment="test", auth_mode="local_fixture", origin="http://testserver"),
        ApplicationPorts(
            factory=migration_factory,
            store=LocalObjectStore(tmp_path / "api-objects"),
            fixture_context=TenantContext(
                org_id=args["org_id"], principal_id=args["principal_id"], role=OrgRole.OWNER
            ),
            model_runtime=runtime,
        ),
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as browser:
            await browser.get("/api/v1/session")
            result = await browser.get(f"/api/v1/repositories/{args['repository_id']}/models")
            assert result.status_code == 200
            assert result.json()["profiles"][0]["alias"] == "openai_fast"
            assert result.json()["live_access_verified"] is False
            assert "SYNTHETIC_KEY" not in result.text
            assert "synthetic-openai_fast" not in result.text
            denied = await browser.get(f"/api/v1/repositories/{uuid4()}/models")
            assert denied.status_code == 404
    finally:
        await client.aclose()


@pytest.mark.parametrize("available", [True, False])
async def test_configured_investigation_executes_and_reloads(
    migration_factory, tmp_path, available
):
    import json

    from app.models.budget_tables import embedding_budgets
    from sqlalchemy import update

    payload = {
        "id": "synthetic-investigation-response",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(
                            {
                                "verdict": "benign",
                                "confidence": 0.9,
                                "summary": "Synthetic test only",
                                "reasoning": "No evidence in fixture",
                                "evidence": [],
                            }
                        ),
                    }
                ],
            }
        ],
        "usage": {
            "input_tokens": 10,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 5,
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }
    gateway, args, client, calls = await configured(migration_factory, tmp_path, payload)
    async with migration_factory() as s, s.begin():
        await s.execute(update(embedding_budgets).values(balance_micros=10_000_000))
    runtime = ConfiguredModelRuntime(
        gateway.registry,
        gateway.transports,
        migration_factory,
        gateway.continuations,
        disclosure_policies={(args["org_id"], args["repository_id"]): args["disclosure_policy"]},
    )
    if not available:
        from dataclasses import replace

        from app.contracts.model_configuration import ModelRegistryConfigV1
        from app.services.model_registry import ModelRegistry

        runtime = replace(
            runtime,
            registry=ModelRegistry(
                ModelRegistryConfigV1(), registered_providers=frozenset(), active_prices=frozenset()
            ),
        )
    app = create_app(
        ApiSettings(environment="test", auth_mode="local_fixture", origin="http://testserver"),
        ApplicationPorts(
            factory=migration_factory,
            store=LocalObjectStore(tmp_path / "api-objects"),
            fixture_context=TenantContext(
                org_id=args["org_id"], principal_id=args["principal_id"], role=OrgRole.OWNER
            ),
            model_runtime=runtime,
        ),
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as browser:
            session = await browser.get("/api/v1/session")
            headers = {
                "Origin": "http://testserver",
                "X-CSRF-Token": session.json()["csrf_token"],
                "Idempotency-Key": "configured-investigation",
            }
            base = f"/api/v1/repositories/{args['repository_id']}/investigations"
            created = await browser.post(
                base,
                headers=headers,
                json={
                    "repository_id": str(args["repository_id"]),
                    "query": "Inspect synthetic fixture",
                    "mode": "short_path",
                    "depth": "standard",
                    "max_steps": 2,
                    "budget_limit_micros": 1_000_000,
                },
            )
            assert created.status_code == 201, created.text
            url = f"{base}/{created.json()['id']}"
            if available:

                async def assert_no_checkpoint_lock(request):
                    from sqlalchemy import text

                    async with migration_factory() as observer, observer.begin():
                        await observer.execute(
                            text("SELECT id FROM investigations WHERE id=:id FOR UPDATE NOWAIT"),
                            {"id": created.json()["id"]},
                        )

                client.event_hooks["request"].append(assert_no_checkpoint_lock)
            result = await browser.post(url + "/execute", headers=headers)
            if not available:
                assert result.status_code == 503, result.text
                assert not calls
                return
            assert result.status_code == 200, result.text
            assert calls, result.text
            reloaded = await browser.get(url)
            assert reloaded.status_code == 200
            assert reloaded.json()["summary"]["status"] == "completed", result.text
    finally:
        await client.aclose()
