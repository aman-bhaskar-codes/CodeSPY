"""Native pgvector storage for chunk embeddings against a real PostgreSQL schema."""

import json
from uuid import uuid4

from scripts import phase2_db as harness
from sqlalchemy import text

FKS = ("fk_emb_chunk", "fk_emb_index", "fk_emb_repo", "fk_emb_snapshot")
PREVIOUS = "0040_investigation_runtime_permissions"


def _vec(*lead: float, dim: int = 1024) -> str:
    return json.dumps([*lead, *([0.0] * (dim - len(lead)))])


async def _insert(session, org, index, vec):
    await session.execute(
        text(
            "INSERT INTO chunk_embeddings(org_id,chunk_id,index_id,repository_id,snapshot_id,"
            "embedding_json) VALUES (:o,:c,:i,:r,:s,CAST(:j AS jsonb))"
        ),
        {"o": org, "c": uuid4(), "i": index, "r": uuid4(), "s": uuid4(), "j": vec},
    )


async def _seedless(session):
    # Constraints are dropped inside a transaction that is always rolled back, so rows
    # can be seeded without the full repository/snapshot/chunk graph.
    for fk in FKS:
        await session.execute(text(f"ALTER TABLE chunk_embeddings DROP CONSTRAINT {fk}"))


async def test_insert_trigger_populates_vector_and_orders_by_cosine(migration_factory):
    org, other, index = uuid4(), uuid4(), uuid4()
    async with migration_factory() as session:
        await _seedless(session)
        for vec in (_vec(1.0, 0.0), _vec(0.7, 0.7), _vec(0.0, 1.0)):
            await _insert(session, org, index, vec)
        await _insert(session, other, index, _vec(1.0, 0.0))

        assert (
            await session.scalar(
                text("SELECT count(*) FROM chunk_embeddings WHERE embedding IS NULL")
            )
            == 0
        )
        query = _vec(1.0, 0.0)
        rows = (
            await session.execute(
                text(
                    "SELECT 1-(embedding::vector(1024) <=> CAST(:q AS vector(1024))) AS sim "
                    "FROM chunk_embeddings WHERE org_id=:o AND index_id=:i "
                    "AND vector_dims(embedding)=1024 "
                    "ORDER BY embedding::vector(1024) <=> CAST(:q AS vector(1024)) LIMIT 10"
                ),
                {"q": query, "o": org, "i": index},
            )
        ).all()
        sims = [round(r[0], 4) for r in rows]
        assert len(sims) == 3  # the other tenant's identical vector never appears
        assert sims == sorted(sims, reverse=True)
        assert sims[0] == 1.0 and sims[-1] == 0.0
        await session.rollback()


async def test_hnsw_index_serves_the_1024_dimension_query(migration_factory):
    org, index = uuid4(), uuid4()
    async with migration_factory() as session:
        await _seedless(session)
        await _insert(session, org, index, _vec(1.0, 0.0))
        await session.execute(text("SET LOCAL enable_seqscan=off"))
        plan = "\n".join(
            row[0]
            for row in (
                await session.execute(
                    text(
                        "EXPLAIN SELECT 1 FROM chunk_embeddings WHERE vector_dims(embedding)=1024 "
                        "ORDER BY embedding::vector(1024) <=> CAST(:q AS vector(1024)) LIMIT 5"
                    ),
                    {"q": _vec(1.0, 0.0)},
                )
            ).all()
        )
        assert "ix_chunk_embeddings_hnsw_1024" in plan
        await session.rollback()


async def test_other_dimensions_are_stored_without_the_index(migration_factory):
    org, index = uuid4(), uuid4()
    async with migration_factory() as session:
        await _seedless(session)
        await _insert(session, org, index, _vec(1.0, dim=512))
        assert (
            await session.scalar(text("SELECT vector_dims(embedding) FROM chunk_embeddings")) == 512
        )
        await session.rollback()


def test_roundtrip_downgrade_and_upgrade(db_values):
    name = db_values["CODESPY_TEST_DATABASE"]
    harness.migrate(db_values, name, PREVIOUS, downgrade=True)
    with harness.connect(db_values, name) as c:
        assert c.execute(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name='chunk_embeddings' AND column_name='embedding'"
        ).fetchone() == (0,)
    harness.migrate(db_values, name, "0041_native_vector_embeddings")
    with harness.connect(db_values, name) as c:
        assert c.execute(
            "SELECT count(*) FROM pg_indexes WHERE indexname='ix_chunk_embeddings_hnsw_1024'"
        ).fetchone() == (1,)


async def test_dense_search_expression_executes_with_threshold_and_limit(migration_factory):
    import sqlalchemy as sa
    from app.models.chunk_tables import chunk_embeddings
    from app.services.dense_search import _Vector

    org, index = uuid4(), uuid4()
    async with migration_factory() as session:
        await _seedless(session)
        for vec in (_vec(1.0, 0.0), _vec(0.7, 0.7), _vec(0.0, 1.0)):
            await _insert(session, org, index, vec)
        column = sa.literal_column("chunk_embeddings.embedding")
        query = sa.cast(sa.literal(_vec(1.0, 0.0)), _Vector(1024))
        distance = sa.cast(column, _Vector(1024)).op("<=>")(query).label("distance")
        stmt = (
            sa.select(distance)
            .select_from(chunk_embeddings)
            .where(
                chunk_embeddings.c.org_id == org,
                sa.func.vector_dims(column) == 1024,
                sa.cast(column, _Vector(1024)).op("<=>")(query) <= 1.0 - 0.5,
            )
            .order_by(distance)
            .limit(5)
        )
        sims = [round(1.0 - row[0], 4) for row in (await session.execute(stmt)).all()]
        assert sims == [1.0, 0.7071]  # the orthogonal vector falls below min_score
        await session.rollback()
