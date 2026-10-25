"""Index model metadata stays additive to legacy job projections."""


def test_index_models_bind_new_fields_without_widening_old_job_projection():
    from app.models.chunk_tables import chunk_embeddings, search_indices, source_chunks
    from app.models.index_tables import (
        index_attempt_specs,
        index_jobs,
        index_publication_receipts,
        index_specs,
        retrieval_chunk_sets,
    )
    from app.models.phase2_tables import PHASE2_TABLES

    assert "index_spec_id" not in PHASE2_TABLES["jobs"].c
    assert "index_spec_id" in index_jobs.c and "result_index_id" in index_jobs.c
    assert "scan_spec_id" in index_jobs.c
    assert all(
        "chunk_set_id" in table.c for table in (source_chunks, search_indices, chunk_embeddings)
    )
    assert "safe_content_sha256" in source_chunks.c
    assert index_specs.c.spec_json is not None
    assert retrieval_chunk_sets.c.manifest_json is not None
    assert set(index_attempt_specs.primary_key.columns.keys()) == {"org_id", "id"}
    assert set(index_publication_receipts.primary_key.columns.keys()) == {
        "org_id",
        "job_id",
        "attempt_id",
        "fence",
    }
