import json
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app.contracts.chunking import ChunkKind, SourceChunkV1
from app.contracts.search import (
    ExactSearchRequestV1,
    ExactSearchType,
    LexicalSearchRequestV1,
    SearchScopeV1,
)
from app.domain.errors import ScopeViolation
from app.models.chunk_tables import source_chunks
from app.models.phase2_tables import PHASE2_TABLES, md
from app.models.tables import memberships
from app.services.lexical_search import LexicalSearchService
from app.services.lexical_tokens import (
    extract_path_tokens,
    split_identifier_subwords,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

DATASET_PATH = Path(__file__).resolve().parent.parent / "eval" / "relevance_dataset.json"
RELEVANCE_DATASET = json.loads(DATASET_PATH.read_text())


def test_split_identifier_subwords() -> None:
    assert split_identifier_subwords("getUserById") == ["get", "user", "by", "id"]
    assert split_identifier_subwords("PaymentTransactionManager") == [
        "payment",
        "transaction",
        "manager",
    ]
    assert split_identifier_subwords("auth_service_v2") == ["auth", "service", "v2"]
    assert split_identifier_subwords("APIKeySecret") == ["api", "key", "secret"]
    assert split_identifier_subwords("") == []


def test_extract_path_tokens() -> None:
    tokens = extract_path_tokens("backend/app/services/source_view.py")
    assert "backend" in tokens
    assert "app" in tokens
    assert "services" in tokens
    assert "source_view" in tokens
    assert "source" in tokens
    assert "view" in tokens
    assert "py" in tokens


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    grants = PHASE2_TABLES["repository_grants"]
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: md.create_all(
                sync_conn,
                tables=[memberships, grants, source_chunks],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


def make_dummy_chunk(
    org_id: UUID,
    repo_id: UUID,
    snap_id: UUID,
    file_id: UUID,
    file_path: str,
    idx: int,
    kind: ChunkKind,
    symbol: str | None,
    qual: str | None,
    content: str,
) -> SourceChunkV1:
    return SourceChunkV1(
        chunk_id=uuid4(),
        org_id=org_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        file_id=file_id,
        file_path=file_path,
        chunk_index=idx,
        chunk_kind=kind,
        symbol_name=symbol,
        qualified_name=qual,
        cpg_node_id=uuid4(),
        start_byte=0,
        end_byte=len(content.encode()),
        start_line=1,
        end_line=10,
        content_sha256="a" * 64,
        byte_count=len(content.encode()),
        token_count=len(content.split()),
        raw_content=content.encode(),
        safe_content=content,
        redaction_digest="b" * 64,
        chunker_version="v1_syntax_aware",
    )


@pytest.mark.asyncio
async def test_exact_search_symbol_and_path(session: AsyncSession) -> None:
    org_id = uuid4()
    principal_id = uuid4()
    repo_id = uuid4()
    snap_id = uuid4()

    # Authorize member
    await session.execute(
        memberships.insert().values(org_id=org_id, principal_id=principal_id, role="member")
    )
    grants = PHASE2_TABLES["repository_grants"]
    await session.execute(
        grants.insert().values(
            org_id=org_id,
            repository_id=repo_id,
            principal_id=principal_id,
            permission="read",
            revoked_at=None,
        )
    )
    await session.commit()

    svc = LexicalSearchService(session)
    c1 = make_dummy_chunk(
        org_id,
        repo_id,
        snap_id,
        uuid4(),
        "backend/app/services/auth.py",
        0,
        ChunkKind.FUNCTION,
        "authenticate_user_credentials",
        "app.services.auth.authenticate_user_credentials",
        "def authenticate_user_credentials(user, password):\n    return True\n",
    )
    c2 = make_dummy_chunk(
        org_id,
        repo_id,
        snap_id,
        uuid4(),
        "frontend/src/services/billing.ts",
        0,
        ChunkKind.CLASS,
        "PaymentTransactionManager",
        "src.services.billing.PaymentTransactionManager",
        "class PaymentTransactionManager {\n    process() {}\n}\n",
    )
    await svc.index_chunks([c1, c2])

    scope = SearchScopeV1(
        org_id=org_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        principal_id=principal_id,
    )

    # 1. Search by exact symbol
    req_sym = ExactSearchRequestV1(
        scope=scope,
        query="authenticate_user_credentials",
        search_type=ExactSearchType.SYMBOL,
    )
    res_sym = await svc.search_exact(req_sym)
    assert len(res_sym.hits) == 1
    assert res_sym.hits[0].symbol_name == "authenticate_user_credentials"
    assert "exact_symbol" in res_sym.hits[0].match_reasons

    # 2. Search by exact file path
    req_path = ExactSearchRequestV1(
        scope=scope,
        query="frontend/src/services/billing.ts",
        search_type=ExactSearchType.FILE_PATH,
    )
    res_path = await svc.search_exact(req_path)
    assert len(res_path.hits) == 1
    assert res_path.hits[0].file_path == "frontend/src/services/billing.ts"

    # 3. Search by path segment
    req_seg = ExactSearchRequestV1(
        scope=scope,
        query="services/billing",
        search_type=ExactSearchType.PATH_SEGMENT,
    )
    res_seg = await svc.search_exact(req_seg)
    assert len(res_seg.hits) == 1


@pytest.mark.asyncio
async def test_lexical_full_text_search(session: AsyncSession) -> None:
    org_id = uuid4()
    principal_id = uuid4()
    repo_id = uuid4()
    snap_id = uuid4()

    await session.execute(
        memberships.insert().values(org_id=org_id, principal_id=principal_id, role="member")
    )
    grants = PHASE2_TABLES["repository_grants"]
    await session.execute(
        grants.insert().values(
            org_id=org_id,
            repository_id=repo_id,
            principal_id=principal_id,
            permission="read",
            revoked_at=None,
        )
    )
    await session.commit()

    svc = LexicalSearchService(session)
    c1 = make_dummy_chunk(
        org_id,
        repo_id,
        snap_id,
        uuid4(),
        "lib/pricing/calculator.js",
        0,
        ChunkKind.FUNCTION,
        "computeInvoiceTotals",
        "pricing.computeInvoiceTotals",
        "function computeInvoiceTotals(items, discountRate, taxRate) {\n    return total;\n}\n",
    )
    c2 = make_dummy_chunk(
        org_id,
        repo_id,
        snap_id,
        uuid4(),
        "lib/user/profile.js",
        0,
        ChunkKind.FUNCTION,
        "renderUserProfile",
        "user.renderUserProfile",
        "function renderUserProfile(user) {\n    return avatar;\n}\n",
    )
    await svc.index_chunks([c1, c2])

    scope = SearchScopeV1(
        org_id=org_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        principal_id=principal_id,
    )

    req = LexicalSearchRequestV1(scope=scope, query="calculate invoice discount tax rate")
    res = await svc.search_lexical(req)

    assert len(res.hits) >= 1
    # First hit must be the pricing calculator
    assert res.hits[0].symbol_name == "computeInvoiceTotals"
    assert "calculator.js" in res.hits[0].file_path


@pytest.mark.asyncio
async def test_tenant_isolation_never_leaks_chunks(session: AsyncSession) -> None:
    org_a = uuid4()
    org_b = uuid4()
    principal_a = uuid4()
    repo_a = uuid4()
    repo_b = uuid4()
    snap_a = uuid4()
    snap_b = uuid4()

    # Principal A only belongs to org A
    await session.execute(
        memberships.insert().values(org_id=org_a, principal_id=principal_a, role="member")
    )
    grants = PHASE2_TABLES["repository_grants"]
    await session.execute(
        grants.insert().values(
            org_id=org_a,
            repository_id=repo_a,
            principal_id=principal_a,
            permission="read",
            revoked_at=None,
        )
    )
    await session.commit()

    svc = LexicalSearchService(session)

    # Chunks in Org A
    c_a = make_dummy_chunk(
        org_a,
        repo_a,
        snap_a,
        uuid4(),
        "src/auth.py",
        0,
        ChunkKind.FUNCTION,
        "get_token",
        "auth.get_token",
        "def get_token(): return 'secret_a'\n",
    )
    # Identical keywords in Org B (private tenant)
    c_b = make_dummy_chunk(
        org_b,
        repo_b,
        snap_b,
        uuid4(),
        "src/auth.py",
        0,
        ChunkKind.FUNCTION,
        "get_token",
        "auth.get_token",
        "def get_token(): return 'confidential_tenant_b'\n",
    )
    await svc.index_chunks([c_a, c_b])

    # Search as Principal A in Scope A
    scope_a = SearchScopeV1(
        org_id=org_a,
        repository_id=repo_a,
        snapshot_id=snap_a,
        principal_id=principal_a,
    )
    res = await svc.search_lexical(LexicalSearchRequestV1(scope=scope_a, query="get_token"))
    assert len(res.hits) == 1
    assert "secret_a" in res.hits[0].snippet
    assert "confidential_tenant_b" not in res.hits[0].snippet

    # Searching in Scope B raises ScopeViolation
    scope_b = SearchScopeV1(
        org_id=org_b,
        repository_id=repo_b,
        snapshot_id=snap_b,
        principal_id=principal_a,
    )
    with pytest.raises(ScopeViolation):
        await svc.search_lexical(LexicalSearchRequestV1(scope=scope_b, query="get_token"))


@pytest.mark.asyncio
async def test_relevance_dataset_evaluation(session: AsyncSession) -> None:
    """Evaluate lexical search against frozen relevance benchmark dataset."""
    dataset = RELEVANCE_DATASET

    org_id = uuid4()
    principal_id = uuid4()
    repo_id = uuid4()
    snap_id = uuid4()

    await session.execute(
        memberships.insert().values(org_id=org_id, principal_id=principal_id, role="member")
    )
    grants = PHASE2_TABLES["repository_grants"]
    await session.execute(
        grants.insert().values(
            org_id=org_id,
            repository_id=repo_id,
            principal_id=principal_id,
            permission="read",
            revoked_at=None,
        )
    )
    await session.commit()

    # Index chunks corresponding to ground truth spans
    test_chunks = [
        make_dummy_chunk(
            org_id,
            repo_id,
            snap_id,
            uuid4(),
            "backend/app/services/auth.py",
            0,
            ChunkKind.FUNCTION,
            "authenticate_user_credentials",
            "app.services.auth.authenticate_user_credentials",
            "def authenticate_user_credentials(user, password):\n    return verify(password)\n",
        ),
        make_dummy_chunk(
            org_id,
            repo_id,
            snap_id,
            uuid4(),
            "frontend/src/services/billing.ts",
            0,
            ChunkKind.CLASS,
            "PaymentTransactionManager",
            "src.services.billing.PaymentTransactionManager",
            "class PaymentTransactionManager {\n    processPayment() {}\n}\n",
        ),
        make_dummy_chunk(
            org_id,
            repo_id,
            snap_id,
            uuid4(),
            "lib/pricing/calculator.js",
            0,
            ChunkKind.FUNCTION,
            "computeInvoiceTotals",
            "pricing.computeInvoiceTotals",
            "function computeInvoiceTotals(items, discount, tax) {\n    return totals;\n}\n",
        ),
        make_dummy_chunk(
            org_id,
            repo_id,
            snap_id,
            uuid4(),
            "backend/app/api/validators.py",
            0,
            ChunkKind.FUNCTION,
            "validate_payload",
            "backend.validators.validate_payload",
            "def validate_payload(data):\n    pass\n",
        ),
        make_dummy_chunk(
            org_id,
            repo_id,
            snap_id,
            uuid4(),
            "frontend/src/utils/validators.ts",
            0,
            ChunkKind.FUNCTION,
            "validate_payload",
            "frontend.validators.validate_payload",
            "function validate_payload(data: any): boolean {\n    return true;\n}\n",
        ),
        make_dummy_chunk(
            org_id,
            repo_id,
            snap_id,
            uuid4(),
            "backend/app/ports/object_store.py",
            0,
            ChunkKind.FUNCTION,
            "open_verified",
            "ports.object_store.open_verified",
            "def open_verified(storage_key: str, checksum: str):\n"
            '    """Open stream from ObjectStore and verify sha256 checksum."""\n'
            "    pass\n",
        ),
        make_dummy_chunk(
            org_id,
            repo_id,
            snap_id,
            uuid4(),
            "backend/app/services/findings_query.py",
            0,
            ChunkKind.FUNCTION,
            "query_finding_details",
            "services.findings_query.query_finding_details",
            "def query_finding_details(finding_id: str):\n"
            "    # codespy_sec_canary SQL injection raw query\n"
            "    stmt = f'SELECT * FROM findings WHERE id = {finding_id}'\n"
            "    return run(stmt)\n",
        ),
        make_dummy_chunk(
            org_id,
            repo_id,
            snap_id,
            uuid4(),
            "backend/app/services/source_view.py",
            0,
            ChunkKind.CLASS,
            "SourceViewService",
            "services.source_view.SourceViewService",
            "class SourceViewService:\n"
            "    def verify_scope(self, scope):\n"
            "        pass\n"
            "    def get_derived_artifacts(self):\n"
            "        pass\n",
        ),
    ]

    svc = LexicalSearchService(session)
    await svc.index_chunks(test_chunks)

    scope = SearchScopeV1(
        org_id=org_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        principal_id=principal_id,
    )

    answerable_total = 0
    hits_top5 = 0

    for case in dataset["cases"]:
        if not case["expected_answerable"]:
            # Negative / unanswerable query: verify no false positive top matches
            res_neg = await svc.search_exact(ExactSearchRequestV1(scope=scope, query=case["query"]))
            assert len(res_neg.hits) == 0
            continue

        answerable_total += 1
        # Try exact search first, then lexical
        res = await svc.search_exact(
            ExactSearchRequestV1(scope=scope, query=case["query"], limit=5)
        )
        if not res.hits:
            res = await svc.search_lexical(
                LexicalSearchRequestV1(scope=scope, query=case["query"], limit=5)
            )

        if res.hits:
            expected_paths = {span["file_path"] for span in case["relevant_spans"]}
            top_paths = {h.file_path for h in res.hits}
            if expected_paths.intersection(top_paths):
                hits_top5 += 1

    recall_at_5 = hits_top5 / answerable_total if answerable_total > 0 else 1.0
    # Achieve Recall@5 >= 0.85 on test benchmark
    assert recall_at_5 >= 0.85, f"Recall@5 {recall_at_5} below target threshold 0.85"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["exact", "lexical"])
async def test_same_org_foreign_repository_snapshot_is_not_searchable(
    session: AsyncSession, mode: str
) -> None:
    org_id, principal_id, allowed_repo, foreign_repo, foreign_snap = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    await session.execute(
        memberships.insert().values(org_id=org_id, principal_id=principal_id, role="member")
    )
    await session.execute(
        PHASE2_TABLES["repository_grants"]
        .insert()
        .values(
            org_id=org_id, repository_id=allowed_repo, principal_id=principal_id, permission="read"
        )
    )
    svc = LexicalSearchService(session)
    foreign_chunk = make_dummy_chunk(
        org_id,
        foreign_repo,
        foreign_snap,
        uuid4(),
        "src/private.py",
        0,
        ChunkKind.FUNCTION,
        "private_function",
        "private.private_function",
        "def private_function(): return 'private payload'",
    )
    await svc.index_chunks([foreign_chunk])
    scope = SearchScopeV1(
        org_id=org_id,
        repository_id=allowed_repo,
        snapshot_id=foreign_snap,
        principal_id=principal_id,
    )
    if mode == "exact":
        response = await svc.search_exact(
            ExactSearchRequestV1(scope=scope, query="private_function")
        )
    else:
        response = await svc.search_lexical(
            LexicalSearchRequestV1(scope=scope, query="private_function")
        )
    assert response.hits == ()


@pytest.mark.asyncio
async def test_mcp_without_trusted_identity_cannot_impersonate_member(
    session: AsyncSession,
) -> None:
    from app.mcp.server import ReadOnlyMcpServer

    org_id, principal_id, repo_id, snap_id = uuid4(), uuid4(), uuid4(), uuid4()
    await session.execute(
        memberships.insert().values(org_id=org_id, principal_id=principal_id, role="member")
    )
    await session.execute(
        PHASE2_TABLES["repository_grants"]
        .insert()
        .values(org_id=org_id, repository_id=repo_id, principal_id=principal_id, permission="read")
    )
    svc = LexicalSearchService(session)
    await svc.index_chunks(
        [
            make_dummy_chunk(
                org_id,
                repo_id,
                snap_id,
                uuid4(),
                "src/private.py",
                0,
                ChunkKind.FUNCTION,
                "private_function",
                "private.private_function",
                "def private_function(): return 'private payload'",
            )
        ]
    )
    response = await ReadOnlyMcpServer(lexical_service=svc).handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "search_exact",
                "arguments": {
                    "org_id": str(org_id),
                    "principal_id": str(principal_id),
                    "repository_id": str(repo_id),
                    "snapshot_id": str(snap_id),
                    "query": "private_function",
                },
            },
        }
    )
    assert response["result"]["isError"] is True
    assert "private payload" not in str(response)


@pytest.mark.asyncio
async def test_trusted_mcp_identity_uses_current_repository_grant(
    session: AsyncSession,
) -> None:
    from app.domain.scope import OrgRole, TenantContext
    from app.mcp.server import ReadOnlyMcpServer
    from sqlalchemy import update

    org_id, principal_id, repo_id, snap_id = uuid4(), uuid4(), uuid4(), uuid4()
    await session.execute(
        memberships.insert().values(org_id=org_id, principal_id=principal_id, role="member")
    )
    grants = PHASE2_TABLES["repository_grants"]
    await session.execute(
        grants.insert().values(
            org_id=org_id, repository_id=repo_id, principal_id=principal_id, permission="read"
        )
    )
    svc = LexicalSearchService(session)
    await svc.index_chunks(
        [
            make_dummy_chunk(
                org_id,
                repo_id,
                snap_id,
                uuid4(),
                "src/private.py",
                0,
                ChunkKind.FUNCTION,
                "private_function",
                "private.private_function",
                "def private_function(): return 'private payload'",
            )
        ]
    )
    server = ReadOnlyMcpServer(
        identity=TenantContext(org_id=org_id, principal_id=principal_id, role=OrgRole.MEMBER),
        lexical_service=svc,
    )
    arguments = {
        "repository_id": str(repo_id),
        "snapshot_id": str(snap_id),
        "query": "private_function",
    }
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "search_exact", "arguments": arguments},
    }
    response = await server.handle_request(request)
    assert response["result"]["isError"] is False
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["hits"][0]["file_path"] == "src/private.py"
    assert payload["scope"]["principal_id"] == str(principal_id)

    for field in ("principal_id", "org_id"):
        spoof = {
            **request,
            "params": {"name": "search_exact", "arguments": {**arguments, field: str(uuid4())}},
        }
        denied = await server.handle_request(spoof)
        assert denied["result"]["isError"] is True
        assert "private payload" not in str(denied)

    from datetime import UTC, datetime

    await session.execute(
        update(grants)
        .where(
            grants.c.org_id == org_id,
            grants.c.repository_id == repo_id,
            grants.c.principal_id == principal_id,
        )
        .values(revoked_at=datetime.now(UTC))
    )
    denied = await server.handle_request(request)
    assert denied["result"]["isError"] is True
    assert "private payload" not in str(denied)
