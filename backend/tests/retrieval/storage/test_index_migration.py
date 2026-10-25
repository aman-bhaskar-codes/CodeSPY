"""Real indexing migration boundaries; no provider or worker simulation claims."""

import pytest
from scripts import phase2_db as harness
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError


async def test_new_storage_and_scoped_columns_are_present(migration_factory):
    async with migration_factory() as session:
        for table in (
            "index_specs",
            "index_attempt_specs",
            "retrieval_chunk_sets",
            "index_publication_receipts",
        ):
            assert await session.scalar(
                text("SELECT to_regclass(:name)"), {"name": "public." + table}
            )
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version"))
            == "0023_index_publication"
        )
        for table, column in (
            ("jobs", "index_spec_id"),
            ("jobs", "result_index_id"),
            ("source_chunks", "chunk_set_id"),
            ("source_chunks", "safe_content_sha256"),
            ("search_indices", "chunk_set_id"),
            ("chunk_embeddings", "chunk_set_id"),
        ):
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name=:table AND column_name=:column"
                    ),
                    {"table": table, "column": column},
                )
                == 1
            )


def test_empty_roundtrip_preserves_prior_revision(db_values):
    name = db_values["CODESPY_TEST_DATABASE"]
    harness.migrate(db_values, name, "0022_triage_commands", downgrade=True)
    with harness.connect(db_values, name) as connection:
        assert (
            connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            == "0022_triage_commands"
        )
        assert connection.execute("SELECT to_regclass('public.index_specs')").fetchone()[0] is None
    harness.migrate(db_values, name, "0023_index_publication")


@pytest.mark.parametrize(
    "table", ["retrieval_chunk_sets", "index_publication_receipts", "index_attempt_specs"]
)
async def test_api_role_cannot_insert_worker_results(api_factory, table):
    async with api_factory() as session:
        with pytest.raises(DBAPIError, match="permission denied"):
            await session.execute(text("INSERT INTO public." + table + " DEFAULT VALUES"))
        await session.rollback()
        assert await session.scalar(text("SELECT 1")) == 1


async def test_legacy_chunk_and_memory_reference_survive_roundtrip(
    migration_factory, db_values, tmp_path
):
    import hashlib
    from uuid import uuid4

    from app.models.chunk_tables import source_chunks
    from app.models.memory_tables import repo_memories
    from app.models.tables import source_files
    from sqlalchemy import select
    from tests.phase3.integration.test_structural_publication import admitted

    ctx, snapshot, repo, _ = await admitted(migration_factory, tmp_path)
    chunk_id, memory_id = uuid4(), uuid4()
    content = "print(1)\n"
    digest = hashlib.sha256(content.encode()).hexdigest()
    async with migration_factory() as session, session.begin():
        file = (
            (
                await session.execute(
                    select(source_files).where(source_files.c.snapshot_id == snapshot)
                )
            )
            .mappings()
            .one()
        )
        pointer = [
            {
                "file_path": file["path"],
                "start_line": 1,
                "end_line": 1,
                "chunk_id": str(chunk_id),
                "sha256": digest,
            }
        ]
        await session.execute(
            source_chunks.insert().values(
                org_id=ctx.org_id,
                id=chunk_id,
                repository_id=repo,
                snapshot_id=snapshot,
                file_id=file["id"],
                file_path=file["path"],
                chunk_index=0,
                chunk_kind="file",
                start_byte=0,
                end_byte=len(content),
                start_line=1,
                end_line=1,
                content_sha256=digest,
                byte_count=len(content),
                estimated_tokens=4,
                safe_content=content,
                redaction_digest="0" * 64,
                chunker_version="legacy-fixture",
            )
        )
        await session.execute(
            repo_memories.insert().values(
                org_id=ctx.org_id,
                id=memory_id,
                repository_id=repo,
                snapshot_id=snapshot,
                memory_kind="architecture_fact",
                topic="synthetic legacy",
                summary="legacy identity",
                evidence_pointers=pointer,
            )
        )
    name = db_values["CODESPY_TEST_DATABASE"]
    harness.migrate(db_values, name, "0022_triage_commands", downgrade=True)
    harness.migrate(db_values, name, "0023_index_publication")
    async with migration_factory() as session:
        chunk = (
            (await session.execute(select(source_chunks).where(source_chunks.c.id == chunk_id)))
            .mappings()
            .one()
        )
        assert chunk["safe_content"] == content and chunk["content_sha256"] == digest
        assert chunk["chunk_set_id"] is None and chunk["safe_content_sha256"] is None
        assert (
            await session.scalar(
                select(repo_memories.c.evidence_pointers).where(repo_memories.c.id == memory_id)
            )
            == pointer
        )
