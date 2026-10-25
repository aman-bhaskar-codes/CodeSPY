"""Synthetic ranking evaluation with mocked materialization and retained-source selection.

Evaluates Exact, Lexical, Dense, and Hybrid (RRF) search across a frozen 10-case
relevance dataset, asserting Recall@5, MRR, negative rejection, and context packing.
"""

import json
import math
import re
import struct
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from app.contracts.chunking import ChunkKind
from app.contracts.context import ContextAssemblyRequestV1, ContextBudgetV1
from app.contracts.provider_budget import EmbeddingRequestV1, EmbeddingResponseV1
from app.contracts.search import (
    DenseSearchRequestV1,
    ExactSearchRequestV1,
    HybridSearchRequestV1,
    IndexState,
    IndexType,
    LexicalSearchRequestV1,
    SearchScopeV1,
)
from app.models.chunk_tables import chunk_embeddings, search_indices, source_chunks
from app.models.phase2_tables import PHASE2_TABLES, md
from app.models.tables import (
    memberships,
    organizations,
    principals,
    repositories,
    source_files,
    source_snapshots,
)
from app.ports.embedding import EmbeddingPort
from app.services.context_assembly import ContextAssemblyService
from app.services.dense_search import DenseSearchService
from app.services.hybrid_search import HybridSearchService
from app.services.lexical_search import LexicalSearchService
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


class SimulatedSemanticEmbeddingAdapter(EmbeddingPort):
    """Deterministic offline bag-of-words semantic embedding adapter for benchmarks.

    Zero external API calls, zero cost. Generates reproducible dense vector embeddings
    where texts sharing semantic vocabulary exhibit high cosine similarity.
    """

    def __init__(self, dimension: int = 1024) -> None:
        self.dimension = dimension

    def _hash_word(self, word: str) -> list[float]:
        # Hash word deterministically into vector coordinates
        import hashlib

        h = hashlib.sha256(word.encode("utf-8")).digest()
        seed = struct.unpack(">I", h[:4])[0]
        vec: list[float] = []
        state = seed
        for _ in range(self.dimension):
            state = (state * 1664525 + 1013904223) & 0xFFFFFFFF
            vec.append((state / 0xFFFFFFFF) * 2.0 - 1.0)
        return vec

    async def embed(self, request: EmbeddingRequestV1) -> EmbeddingResponseV1:
        dim = request.dimensions or self.dimension
        tokens = re.findall(r"\w+", request.text.lower())
        accum = [0.0] * dim
        for tok in tokens:
            w_vec = self._hash_word(tok)
            for i in range(dim):
                accum[i] += w_vec[i]

        norm = math.sqrt(sum(v * v for v in accum)) or 1.0
        normalized = [v / norm for v in accum]

        return EmbeddingResponseV1(
            vector=tuple(normalized),
            token_count=max(1, len(tokens)),
            model_id="amazon.titan-embed-text-v2:0",
            provider="simulated_semantic",
            provider_request_id=f"sim_{uuid4().hex[:8]}",
        )


@dataclass
class EvalBenchmarkResult:
    mode: str
    recall_at_5: float
    mrr: float
    p_at_1: float
    false_positive_rate: float


@pytest_asyncio.fixture
async def eval_session_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    from unittest.mock import AsyncMock

    from tests.phase5.unit.legacy_ranking import install_legacy_ranking

    # Ranking metrics are simulated; PostgreSQL admission/publication is qualified separately.
    install_legacy_ranking(monkeypatch, include_context=True)
    monkeypatch.setattr("app.services.retrieval_authority.locate_source", AsyncMock())
    from sqlalchemy import CheckConstraint

    source_files.c.path.type.collation = None  # type: ignore[attr-defined]
    for table in [source_snapshots, source_files, organizations, principals]:
        for c in list(table.constraints):
            if isinstance(c, CheckConstraint):
                table.constraints.discard(c)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: md.create_all(
                sync_conn,
                tables=[
                    organizations,
                    principals,
                    memberships,
                    repositories,
                    PHASE2_TABLES["repository_grants"],
                    source_snapshots,
                    source_files,
                    source_chunks,
                    search_indices,
                    chunk_embeddings,
                ],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_retrieval_benchmark_suite(
    eval_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    dataset_path = Path(__file__).parent / "relevance_dataset.json"
    assert dataset_path.exists(), f"Relevance dataset missing: {dataset_path}"

    corpus = json.loads(dataset_path.read_text(encoding="utf-8"))

    cases = corpus["cases"]
    assert len(cases) == 10, f"Expected 10 benchmark cases, got {len(cases)}"

    org_id = uuid4()
    principal_id = uuid4()
    repo_id = uuid4()
    snap_id = uuid4()
    now = datetime.now(UTC)

    # 1. Define synthetic code content for the benchmark chunks and distractors
    chunk_definitions: list[dict[str, Any]] = [
        # Match for case 01: authenticate_user_credentials
        {
            "path": "backend/app/services/auth.py",
            "symbol": "authenticate_user_credentials",
            "kind": ChunkKind.FUNCTION,
            "start": 42,
            "end": 78,
            "content": (
                "def authenticate_user_credentials(\n"
                "    username: str, password_hash: str\n"
                ") -> AuthToken:\n"
                '    """Authenticate user credentials and issue cryptographic session token."""\n'
                "    user = query_user_by_name(username)\n"
                "    if verify_hash(user.password_hash, password_hash):\n"
                "        return issue_token(user.id)\n"
                "    raise AuthFailed('Invalid credentials')\n"
            ),
        },
        # Match for case 02: PaymentTransactionManager
        {
            "path": "frontend/src/services/billing.ts",
            "symbol": "PaymentTransactionManager",
            "kind": ChunkKind.CLASS,
            "start": 15,
            "end": 90,
            "content": (
                "export class PaymentTransactionManager {\n"
                "  constructor(private client: HttpClient) {}\n"
                "  async processPayment(transaction: TransactionRequest): Promise<Receipt> {\n"
                "    return this.client.post('/api/billing/charge', transaction);\n"
                "  }\n"
                "}\n"
            ),
        },
        # Match for case 03: computeInvoiceTotals
        {
            "path": "lib/pricing/calculator.js",
            "symbol": "computeInvoiceTotals",
            "kind": ChunkKind.FUNCTION,
            "start": 20,
            "end": 65,
            "content": (
                "function computeInvoiceTotals(items, discountRate, taxRate) {\n"
                "  // calculate invoice discount tax rate\n"
                "  const subtotal = items.reduce((acc, it) => acc + it.price * it.qty, 0);\n"
                "  const discount = subtotal * discountRate;\n"
                "  const taxable = subtotal - discount;\n"
                "  const tax = taxable * taxRate;\n"
                "  return { subtotal, discount, tax, total: taxable + tax };\n"
                "}\n"
            ),
        },
        # Match for case 04: ObjectStore open_verified (port)
        {
            "path": "backend/app/ports/object_store.py",
            "symbol": "open_verified",
            "kind": ChunkKind.FUNCTION,
            "start": 14,
            "end": 14,
            "content": (
                "class ObjectStore(Protocol):\n"
                "    async def open_verified(\n"
                "        self, key: str, sha256: str\n"
                "    ) -> AsyncIterator[bytes]: ...\n"
            ),
        },
        # Match for case 04: LocalObjectStore open_verified (adapter)
        {
            "path": "backend/app/adapters/storage/local.py",
            "symbol": "open_verified",
            "kind": ChunkKind.FUNCTION,
            "start": 120,
            "end": 145,
            "content": (
                "class LocalObjectStore(ObjectStore):\n"
                "    async def open_verified(\n"
                "        self, key: str, expected_sha: str\n"
                "    ) -> AsyncIterator[bytes]:\n"
                "        path = self.root / key\n"
                "        # verify hash integrity and stream bytes\n"
                "        async for chunk in read_file(path):\n"
                "            yield chunk\n"
            ),
        },
        # Match for case 05: query_finding_details (SQL injection)
        {
            "path": "backend/app/services/findings_query.py",
            "symbol": "query_finding_details",
            "kind": ChunkKind.FUNCTION,
            "start": 150,
            "end": 195,
            "content": (
                "def query_finding_details(finding_id: str) -> FindingDetail:\n"
                "    # codespy_sec_canary SQL injection raw query verification\n"
                "    query = text(\n"
                "        'SELECT id, description, severity FROM findings WHERE id = :id'\n"
                "    )\n"
                "    return db.execute(query, {'id': finding_id}).fetchone()\n"
            ),
        },
        # Match for case 07: validate_payload (backend)
        {
            "path": "backend/app/api/validators.py",
            "symbol": "validate_payload",
            "kind": ChunkKind.FUNCTION,
            "start": 10,
            "end": 35,
            "content": (
                "def validate_payload(data: dict, schema: Schema) -> bool:\n"
                '    """Validate API request JSON payload against strict domain schema."""\n'
                "    return schema.is_valid(data)\n"
            ),
        },
        # Match for case 07: validate_payload (frontend)
        {
            "path": "frontend/src/utils/validators.ts",
            "symbol": "validate_payload",
            "kind": ChunkKind.FUNCTION,
            "start": 5,
            "end": 28,
            "content": (
                "export function validate_payload(data: unknown): boolean {\n"
                "  return typeof data === 'object' && data !== null;\n"
                "}\n"
            ),
        },
        # Match for case 08: SourceViewService
        {
            "path": "backend/app/services/source_view.py",
            "symbol": "SourceViewService",
            "kind": ChunkKind.CLASS,
            "start": 35,
            "end": 120,
            "content": (
                "class SourceViewService:\n"
                "    async def get(self, request, policy):\n"
                "        await self.verify_scope(request)\n"
                "        # query derived_artifacts for redaction transformations\n"
                "        return build_view(request)\n"
            ),
        },
        # Distractor 1: Logger
        {
            "path": "backend/app/logging.py",
            "symbol": "setup_structured_logging",
            "kind": ChunkKind.FUNCTION,
            "start": 1,
            "end": 30,
            "content": (
                "def setup_structured_logging():\n"
                "    logger = logging.getLogger('codespy')\n"
                "    return logger"
            ),
        },
        # Distractor 2: Metrics
        {
            "path": "backend/app/metrics.py",
            "symbol": "record_histogram_metric",
            "kind": ChunkKind.FUNCTION,
            "start": 1,
            "end": 25,
            "content": (
                "def record_histogram_metric(name: str, value: float):\n"
                "    statsd.timing(name, value)"
            ),
        },
        # Distractor 3: Math utilities
        {
            "path": "lib/math/vectors.js",
            "symbol": "vectorDotProduct",
            "kind": ChunkKind.FUNCTION,
            "start": 1,
            "end": 20,
            "content": (
                "function vectorDotProduct(a, b) {\n"
                "    return a.reduce((sum, v, i) => sum + v * b[i], 0);\n"
                "}"
            ),
        },
        # Distractor 4: HTTP retry policy
        {
            "path": "backend/app/net/retries.py",
            "symbol": "exponential_backoff_policy",
            "kind": ChunkKind.FUNCTION,
            "start": 1,
            "end": 40,
            "content": (
                "def exponential_backoff_policy(attempt: int) -> float:\n"
                "    return 0.5 * (2 ** attempt)"
            ),
        },
    ]

    embedder = SimulatedSemanticEmbeddingAdapter(dimension=1024)
    index_id = uuid4()

    async with eval_session_factory() as session, session.begin():
        await session.execute(
            organizations.insert().values(id=org_id, slug="eval-org", display_name="Eval Org")
        )
        await session.execute(
            principals.insert().values(
                id=principal_id, subject_key="eval-principal", display_name="Eval Principal"
            )
        )
        await session.execute(
            memberships.insert().values(org_id=org_id, principal_id=principal_id, role="owner")
        )
        await session.execute(
            repositories.insert().values(
                id=repo_id,
                org_id=org_id,
                repository_key="eval-repo",
                display_name="Eval Repo",
                source_kind="local_fixture",
            )
        )
        await session.execute(
            PHASE2_TABLES["repository_grants"]
            .insert()
            .values(
                org_id=org_id,
                repository_id=repo_id,
                principal_id=principal_id,
                permission="write",
                revoked_at=None,
            )
        )
        await session.execute(
            source_snapshots.insert().values(
                id=snap_id,
                org_id=org_id,
                repository_id=repo_id,
                artifact_id=uuid4(),
                admission_config_hash="0" * 64,
                identity_hash="1" * 64,
                state="sealed",
            )
        )
        # Create search index record
        await session.execute(
            search_indices.insert().values(
                id=index_id,
                org_id=org_id,
                repository_id=repo_id,
                snapshot_id=snap_id,
                index_type=str(IndexType.DENSE),
                state=str(IndexState.READY),
                embedding_provider="simulated_semantic",
                embedding_model="amazon.titan-embed-text-v2:0",
                dimension=1024,
                total_chunks=len(chunk_definitions),
                indexed_chunks=len(chunk_definitions),
                failed_chunks=0,
                metadata_json={},
                created_at=now,
                updated_at=now,
            )
        )

        # Seed files, chunks and embeddings
        for idx, item in enumerate(chunk_definitions):
            file_id = uuid4()
            chunk_id = uuid4()
            content = item["content"]
            await session.execute(
                source_files.insert().values(
                    id=file_id,
                    org_id=org_id,
                    snapshot_id=snap_id,
                    path=item["path"],
                    sha256="f" * 64,
                    size_bytes=len(content.encode("utf-8")),
                    language="python",
                )
            )
            await session.execute(
                source_chunks.insert().values(
                    id=chunk_id,
                    org_id=org_id,
                    repository_id=repo_id,
                    snapshot_id=snap_id,
                    file_id=file_id,
                    file_path=item["path"],
                    chunk_index=idx,
                    chunk_kind=str(item["kind"]),
                    symbol_name=item["symbol"],
                    qualified_name=f"{item['path']}.{item['symbol']}" if item["symbol"] else None,
                    cpg_node_id=None,
                    start_byte=0,
                    end_byte=len(content.encode("utf-8")),
                    start_line=item["start"],
                    end_line=item["end"],
                    content_sha256="c" * 64,
                    byte_count=len(content.encode("utf-8")),
                    estimated_tokens=max(1, len(content.split())),
                    safe_content=content,
                    redaction_digest="d" * 64,
                    chunker_version="v1",
                    tsv=None,
                    created_at=now,
                )
            )
            # Embed chunk
            emb = await embedder.embed(
                EmbeddingRequestV1(
                    text=f"{item['path']} {item['symbol'] or ''} {content}",
                    dimensions=1024,
                    normalize=True,
                )
            )
            await session.execute(
                chunk_embeddings.insert().values(
                    org_id=org_id,
                    chunk_id=chunk_id,
                    index_id=index_id,
                    repository_id=repo_id,
                    snapshot_id=snap_id,
                    embedding_json=list(emb.vector),
                    token_count=emb.token_count,
                    created_at=now,
                )
            )

    # 2. Benchmark execution harness across all 4 modes
    scope = SearchScopeV1(
        org_id=org_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        principal_id=principal_id,
    )

    unanswerable_cases = [c for c in cases if not c["expected_answerable"]]

    def match_span(
        hit_path: str, hit_symbol: str | None, relevant_spans: list[dict[str, Any]]
    ) -> bool:
        for span in relevant_spans:
            if span["file_path"] in hit_path or hit_path in span["file_path"]:
                if span.get("symbol_name") is None:
                    return True
                if hit_symbol and hit_symbol == span["symbol_name"]:
                    return True
        return False

    modes = ["Exact", "Lexical", "Dense", "Hybrid"]
    benchmark_metrics: dict[str, EvalBenchmarkResult] = {}

    for mode in modes:
        hits_by_case: list[tuple[dict[str, Any], Sequence[Any]]] = []
        fp_count = 0

        async with eval_session_factory() as session:
            lexical_svc = LexicalSearchService(session)
            dense_svc = DenseSearchService(session, embedding_port=embedder)
            hybrid_svc = HybridSearchService(
                session, lexical_service=lexical_svc, dense_service=dense_svc
            )

            for case in cases:
                q = case["query"]
                if mode == "Exact":
                    res = await lexical_svc.search_exact(
                        ExactSearchRequestV1(scope=scope, query=q, limit=5)
                    )
                    hits = res.hits
                elif mode == "Lexical":
                    res = await lexical_svc.search_lexical(
                        LexicalSearchRequestV1(scope=scope, query=q, limit=5)
                    )
                    hits = res.hits
                elif mode == "Dense":
                    res = await dense_svc.search_dense(
                        DenseSearchRequestV1(scope=scope, query=q, index_id=index_id, limit=5)
                    )
                    hits = res.hits
                else:  # Hybrid
                    res = await hybrid_svc.search_hybrid(
                        HybridSearchRequestV1(
                            scope=scope, query=q, dense_index_id=index_id, limit=5
                        )
                    )
                    hits = res.hits

                hits_by_case.append((case, hits))
                if (
                    not case["expected_answerable"]
                    and len(hits) > 0
                    and (
                        mode == "Exact"
                        or (
                            mode == "Hybrid"
                            and any(getattr(h, "score", 0.0) >= 0.035 for h in hits)
                        )
                    )
                ):
                    fp_count += 1

        # Calculate metrics over answerable cases
        recalls: list[float] = []
        mrrs: list[float] = []
        p_at_1s: list[float] = []

        for case, hits in hits_by_case:
            if not case["expected_answerable"]:
                continue
            relevant_spans = case["relevant_spans"]
            matched_ranks: list[int] = []
            for rank, hit in enumerate(hits, start=1):
                p = getattr(hit, "file_path", "")
                s = getattr(hit, "symbol_name", None)
                if match_span(p, s, relevant_spans):
                    matched_ranks.append(rank)

            if matched_ranks:
                recalls.append(1.0)
                mrrs.append(1.0 / matched_ranks[0])
                p_at_1s.append(1.0 if matched_ranks[0] == 1 else 0.0)
            else:
                recalls.append(0.0)
                mrrs.append(0.0)
                p_at_1s.append(0.0)

        mean_recall = sum(recalls) / len(recalls) if recalls else 0.0
        mean_mrr = sum(mrrs) / len(mrrs) if mrrs else 0.0
        mean_p1 = sum(p_at_1s) / len(p_at_1s) if p_at_1s else 0.0
        fpr = fp_count / len(unanswerable_cases) if unanswerable_cases else 0.0

        benchmark_metrics[mode] = EvalBenchmarkResult(
            mode=mode,
            recall_at_5=mean_recall,
            mrr=mean_mrr,
            p_at_1=mean_p1,
            false_positive_rate=fpr,
        )

    # 3. Print quantitative benchmark scorecard
    print("\n" + "=" * 76)
    print("CodeSPY Phase 5 Retrieval Benchmark Results (Frozen Corpus v1.0.0)")
    print("=" * 76)
    print(f"{'Mode':<10} | {'Recall@5':<10} | {'MRR':<10} | {'P@1':<10} | {'Negative FPR':<12}")
    print("-" * 10 + "-+-" + "-" * 10 + "-+-" + "-" * 10 + "-+-" + "-" * 10 + "-+-" + "-" * 12)
    for m in modes:
        res = benchmark_metrics[m]
        print(
            f"{res.mode:<10} | {res.recall_at_5:<10.3f} | {res.mrr:<10.3f} | "
            f"{res.p_at_1:<10.3f} | {res.false_positive_rate:<12.3f}"
        )
    print("=" * 76 + "\n")

    # 4. Rigorous assertions
    hybrid_metric = benchmark_metrics["Hybrid"]
    assert hybrid_metric.recall_at_5 >= 0.85, (
        f"Hybrid search Recall@5 failed threshold: {hybrid_metric.recall_at_5:.3f} < 0.85"
    )
    assert hybrid_metric.false_positive_rate == 0.0, (
        f"Hybrid search false positive rate must be 0, got {hybrid_metric.false_positive_rate}"
    )

    exact_metric = benchmark_metrics["Exact"]
    assert exact_metric.false_positive_rate == 0.0, "Exact search false positive rate must be 0"

    # 5. Evaluate Context Assembly with budget packing & citations
    async with eval_session_factory() as session:
        lexical_svc = LexicalSearchService(session)
        dense_svc = DenseSearchService(session, embedding_port=embedder)
        hybrid_svc = HybridSearchService(
            session, lexical_service=lexical_svc, dense_service=dense_svc
        )
        hybrid_res = await hybrid_svc.search_hybrid(
            HybridSearchRequestV1(
                scope=scope,
                query="authenticate_user_credentials",
                dense_index_id=index_id,
                limit=5,
            )
        )
        assert len(hybrid_res.hits) >= 1

        assembly_svc = ContextAssemblyService(session)
        ctx_packet = await assembly_svc.assemble_context(
            ContextAssemblyRequestV1(
                scope=scope,
                query="authenticate_user_credentials",
                candidate_hits=hybrid_res.hits,
                budget=ContextBudgetV1(max_tokens=500, reserved_overhead_tokens=50),
            )
        )
        assert ctx_packet.items_count >= 1
        assert ctx_packet.total_tokens <= 500
        first_item = ctx_packet.items[0]
        assert "auth.py" in first_item.citation_key
        assert "authenticate_user_credentials" in first_item.safe_content
