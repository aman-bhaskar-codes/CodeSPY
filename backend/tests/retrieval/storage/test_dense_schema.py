"""Dense index schema (revision 0044): constraints, uniqueness and downgrade safety."""

import os
from uuid import uuid4, uuid5

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from tests.retrieval.storage.test_index_guards import (
    owned_attempt,
    publish_rows,
    set_worker,
    source_case,
    terminalize,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("CODESPY_TEST_SCHEMA_TARGET") != "head",
    reason="requires the current schema head (revision 0044)",
)


async def published_lexical(api_factory, broker_factory, migration_factory, tmp_path):
    from app.models.index_tables import index_specs

    ctx, spec, values = await source_case(migration_factory, tmp_path)
    async with api_factory() as session, session.begin():
        await session.execute(index_specs.insert().values(**values))
    binding = await owned_attempt(migration_factory, ctx, spec, values["id"])
    async with broker_factory() as session, session.begin():
        await set_worker(session, ctx.org_id, binding)
        draft, receipt = await publish_rows(session, spec, values["id"], binding)
        await terminalize(session, ctx.org_id, binding, receipt["index_id"])
    return ctx, spec, draft, receipt


def dense_id(set_id, provider, model, dimension):
    return uuid5(set_id, f"dense-v1:{provider}:{model}:{dimension}")


def index_row(spec, set_id, index_id, **overrides):
    row = {
        "org_id": spec.org_id,
        "id": index_id,
        "repository_id": spec.repository_id,
        "snapshot_id": spec.snapshot_id,
        "index_type": "dense",
        "state": "ready",
        "embedding_provider": "aws_bedrock",
        "embedding_model": "amazon.titan-embed-text-v2:0",
        "dimension": 1024,
        "chunk_set_id": set_id,
        "total_chunks": 1,
        "indexed_chunks": 1,
        "failed_chunks": 0,
    }
    return {**row, **overrides}


INSERT = text(
    "INSERT INTO search_indices(org_id,id,repository_id,snapshot_id,index_type,state,"
    "embedding_provider,embedding_model,dimension,chunk_set_id,total_chunks,indexed_chunks,"
    "failed_chunks,sealed_at) "
    "VALUES(:org_id,:id,:repository_id,:snapshot_id,:index_type,:state,:embedding_provider,"
    ":embedding_model,:dimension,:chunk_set_id,:total_chunks,:indexed_chunks,:failed_chunks,"
    "clock_timestamp())"
)


GUARDS = ("guard_index_result", "guard_index_committed")


async def without_row_guards(session):
    """Schema tests exercise table constraints; the publication guards have their own tests.

    The migration role owns the table, so it may disable the triggers inside its transaction.
    """
    for trigger in GUARDS:
        await session.execute(text(f"ALTER TABLE public.search_indices DISABLE TRIGGER {trigger}"))


async def restore_row_guards(session):
    for trigger in GUARDS:
        await session.execute(text(f"ALTER TABLE public.search_indices ENABLE TRIGGER {trigger}"))


async def definition(session, query, **params):
    return (await session.execute(text(query), params)).scalars().all()


async def test_schema_objects_and_constraint_shapes(migration_factory):
    async with migration_factory() as session:
        tables = set(
            await definition(
                session,
                "SELECT table_name FROM information_schema.tables WHERE table_schema='public' "
                "AND table_name IN ('embed_specs','embed_attempt_specs',"
                "'embed_publication_receipts','embed_vector_stage')",
            )
        )
        assert tables == {
            "embed_specs",
            "embed_attempt_specs",
            "embed_publication_receipts",
            "embed_vector_stage",
        }
        ops = await definition(
            session,
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='ck_job_operation'",
        )
        assert "embed_chunk_set" in ops[0]
        qualified = await definition(
            session,
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname='ck_index_qualified'",
        )
        assert "dense-v1" in qualified[0] and "lexical-v1" in qualified[0]
        indexes = set(
            await definition(
                session,
                "SELECT indexname FROM pg_indexes WHERE tablename='search_indices' "
                "AND indexname LIKE 'uq_index_set_%'",
            )
        )
        assert indexes == {"uq_index_set_scope", "uq_index_set_lexical", "uq_index_set_dense"}


async def test_dense_row_shape_is_constrained_and_profiles_may_coexist(
    api_factory, broker_factory, migration_factory, tmp_path
):
    _, spec, _, receipt = await published_lexical(
        api_factory, broker_factory, migration_factory, tmp_path
    )
    set_id = receipt["chunk_set_id"]
    ok = index_row(
        spec, set_id, dense_id(set_id, "aws_bedrock", "amazon.titan-embed-text-v2:0", 1024)
    )
    async with migration_factory() as session:
        await without_row_guards(session)
        await session.execute(INSERT, ok)
        # a second profile (other dimension) on the same set is allowed
        await session.execute(
            INSERT,
            index_row(
                spec,
                set_id,
                dense_id(set_id, "aws_bedrock", "amazon.titan-embed-text-v2:0", 512),
                dimension=512,
            ),
        )
        for bad in (
            index_row(spec, set_id, uuid4()),  # wrong deterministic id
            index_row(spec, set_id, ok["id"], dimension=300),  # unsupported dimension
            index_row(spec, set_id, ok["id"], embedding_provider=None),
            index_row(spec, set_id, ok["id"], state="building"),
        ):
            with pytest.raises((IntegrityError, DBAPIError)):
                async with session.begin_nested():
                    await session.execute(INSERT, bad)
        # The same profile twice is impossible: the deterministic id makes it a primary-key
        # conflict, and a different id fails the shape check.
        for duplicate in (ok, {**ok, "id": uuid4()}):
            with pytest.raises((IntegrityError, DBAPIError)):
                async with session.begin_nested():
                    await session.execute(INSERT, duplicate)
        await session.rollback()


async def test_downgrade_is_refused_while_dense_history_exists_and_round_trips_when_empty(
    api_factory, broker_factory, migration_factory, tmp_path, db_values
):
    from scripts import phase2_db as harness

    name = db_values["CODESPY_TEST_DATABASE"]
    harness.migrate(db_values, name, "0043_ast_file_facts", downgrade=True)
    harness.migrate(db_values, name, "head")
    async with migration_factory() as session:
        assert await session.scalar(text("SELECT to_regclass('public.embed_specs') IS NOT NULL"))

    _, spec, _, receipt = await published_lexical(
        api_factory, broker_factory, migration_factory, tmp_path
    )
    set_id = receipt["chunk_set_id"]
    async with migration_factory() as session, session.begin():
        await without_row_guards(session)
        await session.execute(
            INSERT,
            index_row(
                spec, set_id, dense_id(set_id, "aws_bedrock", "amazon.titan-embed-text-v2:0", 1024)
            ),
        )
        await restore_row_guards(session)
    with pytest.raises(Exception, match="dense index history exists"):
        harness.migrate(db_values, name, "0043_ast_file_facts", downgrade=True)
