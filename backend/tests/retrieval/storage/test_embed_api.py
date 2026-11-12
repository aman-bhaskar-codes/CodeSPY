"""HTTP routes for dense index jobs: CSRF, idempotency, configuration and policy gates."""

import os
from uuid import uuid4

import httpx
import pytest
from app.adapters.storage.local import LocalObjectStore
from app.api.application import ApiSettings, ApplicationPorts, create_app
from app.contracts.embedding_index import EmbedConfigV1
from app.contracts.provider_budget import DisclosurePolicyV1

from tests.retrieval.storage.test_dense_guards import EMBED_POLICY, MODEL, PROVIDER, embed_base

pytestmark = pytest.mark.skipif(
    os.environ.get("CODESPY_TEST_SCHEMA_TARGET") != "head",
    reason="requires the current schema head (revision 0046)",
)

CONFIG = EmbedConfigV1(
    provider=PROVIDER, model_id=MODEL, dimension=512, normalize=True, price_version="test"
)


async def client_for(base, api_factory, tmp_path, *, config=CONFIG, policy=EMBED_POLICY, key="k1"):
    app = create_app(
        ApiSettings(environment="test", auth_mode="local_fixture", origin="http://testserver"),
        ApplicationPorts(
            factory=api_factory,
            store=LocalObjectStore(tmp_path / "objects"),
            fixture_context=base["ctx"],
            embed_config=config,
            embed_policy=(lambda org, repo: policy) if policy is not None else None,
        ),
    )
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    token = (await client.get("/api/v1/session")).json()["csrf_token"]
    headers = {"Origin": "http://testserver", "X-CSRF-Token": token, "Idempotency-Key": key}
    return client, headers


def route(base, chunk_set_id=None, suffix="dense"):
    spec = base["spec"]
    return (
        f"/api/v1/repositories/{spec.repository_id}/snapshots/{spec.snapshot_id}"
        f"/indexes/{chunk_set_id or base['set_id']}/{suffix}"
    )


def job_route(base, job_id, suffix=""):
    spec = base["spec"]
    return (
        f"/api/v1/repositories/{spec.repository_id}/snapshots/{spec.snapshot_id}"
        f"/embed-jobs/{job_id}{suffix}"
    )


async def test_create_status_and_cancel_through_http(
    api_factory, broker_factory, migration_factory, tmp_path
):
    base = await embed_base(api_factory, broker_factory, migration_factory, tmp_path)
    client, headers = await client_for(base, api_factory, tmp_path)
    try:
        body = {"request_key": "k1"}
        assert (await client.post(route(base), json=body)).status_code == 403  # no CSRF
        first = await client.post(route(base), json=body, headers=headers)
        assert first.status_code == 202, first.text
        job = first.json()["job"]
        assert job["state"] == "queued" and job["operation"] == "embed_chunk_set"
        again = await client.post(route(base), json=body, headers=headers)
        assert again.status_code == 202 and again.json()["job"]["id"] == job["id"]
        mismatch = await client.post(route(base), json={"request_key": "other"}, headers=headers)
        assert mismatch.status_code == 422
        status = await client.get(job_route(base, job["id"]))
        assert status.status_code == 200
        assert status.json()["can_cancel"] is True and status.json()["receipt"] is None
        cancelled = await client.post(job_route(base, job["id"], "/cancel"), headers=headers)
        assert cancelled.status_code == 200 and cancelled.json()["job"]["state"] == "cancelled"
        unknown = await client.get(job_route(base, uuid4()))
        assert unknown.status_code == 404
        # scope in the URL must match the job: another repository or snapshot reveals nothing
        other_repo = job_route(base, job["id"]).replace(
            str(base["spec"].repository_id), str(uuid4())
        )
        assert (await client.get(other_repo)).status_code == 404
        assert (await client.post(other_repo + "/cancel", headers=headers)).status_code == 404
        other_snapshot = job_route(base, job["id"], "/cancel").replace(
            str(base["spec"].snapshot_id), str(uuid4())
        )
        assert (await client.post(other_snapshot, headers=headers)).status_code == 404
        reserved = await client.post(
            route(base),
            json={"request_key": "job:x"},
            headers={**headers, "Idempotency-Key": "job:x"},
        )
        assert reserved.status_code == 422
    finally:
        await client.aclose()


async def test_unconfigured_forbidden_and_unknown_requests_are_refused(
    api_factory, broker_factory, migration_factory, tmp_path
):
    base = await embed_base(api_factory, broker_factory, migration_factory, tmp_path)
    body = {"request_key": "k1"}
    client, headers = await client_for(base, api_factory, tmp_path, config=None)
    try:
        unconfigured = await client.post(route(base), json=body, headers=headers)
        assert (
            unconfigured.status_code == 503 and unconfigured.json()["code"] == "embed_unavailable"
        )
    finally:
        await client.aclose()
    client, headers = await client_for(base, api_factory, tmp_path, policy=None)
    try:
        assert (await client.post(route(base), json=body, headers=headers)).status_code == 503
    finally:
        await client.aclose()
    client, headers = await client_for(base, api_factory, tmp_path, policy=DisclosurePolicyV1())
    try:
        denied = await client.post(route(base), json=body, headers=headers)
        assert denied.status_code == 403 and denied.json()["code"] == "disclosure_denied"
    finally:
        await client.aclose()
    client, headers = await client_for(base, api_factory, tmp_path)
    try:
        missing = await client.post(route(base, uuid4()), json=body, headers=headers)
        assert missing.status_code == 404
    finally:
        await client.aclose()


class QueryEmbedderStub:
    """Stands in for the accounted Bedrock query embedder; returns a chosen vector."""

    def __init__(self, vector):
        self.vector, self.calls = vector, []

    async def embed_query(self, scope, request):
        from app.contracts.provider_budget import EmbeddingResponseV1

        self.calls.append((scope.org_id, request.text))
        return EmbeddingResponseV1(
            vector=tuple(self.vector),
            token_count=1,
            model_id=request.model_id,
            provider=request.provider,
        )


async def test_http_create_then_worker_then_status_and_dense_search_end_to_end(
    api_factory, broker_factory, migration_factory, tmp_path
):
    import json

    from app.domain.canonical_json import canonical_bytes
    from app.ports.queue import Delivery
    from app.services.embed_job import EmbedOperation
    from app.workers.consumer import JobConsumer
    from sqlalchemy import text

    from tests.retrieval.storage.test_dense_job import Embedder

    class Queue:
        async def ack(self, receipt):
            pass

        async def extend(self, *args):
            pass

    base = await embed_base(api_factory, broker_factory, migration_factory, tmp_path)
    client, headers = await client_for(base, api_factory, tmp_path)
    try:
        created = await client.post(route(base), json={"request_key": "k1"}, headers=headers)
        assert created.status_code == 202, created.text
        job_id = created.json()["job"]["id"]
        async with migration_factory() as session:
            payload = await session.scalar(
                text("SELECT payload FROM outbox WHERE job_id=:j"), {"j": job_id}
            )
        payload = json.loads(payload) if isinstance(payload, str) else payload
        embedder = Embedder()
        await JobConsumer(
            broker_factory,
            Queue(),
            None,
            None,
            embed_operation=EmbedOperation(
                broker_factory, embedder, policy_for=lambda o, r: EMBED_POLICY
            ),
        ).handle(Delivery(canonical_bytes(payload), "delivery"))
        status = (await client.get(job_route(base, job_id))).json()
        assert status["job"]["state"] == "succeeded" and status["can_cancel"] is False
        receipt = status["receipt"]
        assert receipt["chunk_count"] == len(embedder.calls) and receipt["coverage"] == "complete"

        async with migration_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT chunk_id,embedding_json FROM chunk_embeddings "
                        "WHERE index_id=:i ORDER BY chunk_id LIMIT 1"
                    ),
                    {"i": receipt["index_id"]},
                )
            ).one()
        vector = (
            json.loads(row.embedding_json)
            if isinstance(row.embedding_json, str)
            else row.embedding_json
        )
        stub = QueryEmbedderStub(vector)
        app = create_app(
            ApiSettings(environment="test", auth_mode="local_fixture", origin="http://testserver"),
            ApplicationPorts(
                factory=api_factory,
                store=LocalObjectStore(tmp_path / "objects2"),
                fixture_context=base["ctx"],
                query_embedder=stub,
            ),
        )
        searcher = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        try:
            search_url = (
                f"/api/v1/repositories/{base['spec'].repository_id}/snapshots/"
                f"{base['spec'].snapshot_id}/search/dense"
            )
            token = (await searcher.get("/api/v1/session")).json()["csrf_token"]
            found = await searcher.post(
                search_url,
                json={"query": "where is the entry point", "chunk_set_id": str(base["set_id"])},
                headers={"Origin": "http://testserver", "X-CSRF-Token": token},
            )
            assert found.status_code == 200, found.text
            result = found.json()
            assert result["availability"] == "available" and result["hits"]
            assert result["hits"][0]["chunk_id"] == str(row.chunk_id)
            assert result["hits"][0]["score"] > 0.99 and len(stub.calls) == 1
        finally:
            await searcher.aclose()
    finally:
        await client.aclose()
