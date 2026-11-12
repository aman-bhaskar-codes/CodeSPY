# Native vector storage

Chunk embeddings are stored as native pgvector values alongside the canonical JSON
copy, and dense search ranks by cosine distance in PostgreSQL.

## Implemented boundary

Revision `0041_native_vector_embeddings` adds a dimension-less `embedding vector`
column to `chunk_embeddings`. A row-level insert trigger mirrors `embedding_json`
into it, and existing rows are backfilled. `embedding_json` remains the canonical
copy, so the downgrade drops only derived objects and loses no data.

A partial HNSW index with cosine operators covers the 1024-dimension profile
(`ix_chunk_embeddings_hnsw_1024`, predicate `vector_dims(embedding) = 1024`).
Other dimensions are stored and searched exactly within the scoped rows, without
an index.

On PostgreSQL, `DenseSearchService` orders and limits by `<=>` inside the existing
organisation, repository, snapshot, index and chunk-set filters, and applies
`min_score` as a distance bound. SQLite (unit tests) keeps the in-process cosine
path. Scores are identical in both paths.

## Limitations

- On the PostgreSQL path `total_candidates` is the number of rows returned by the
  query, not every row above `min_score`; counting them all would restore a full scan.
- Filtered HNSW scans can return fewer than `limit` rows when the filters are
  selective. Iterative scans are not enabled.
- Dense publication is possible only through the fenced embed attempt in
  [Dense index publication](dense-index-publication.md), whose job is not yet creatable through the
  API. The PostgreSQL branch of `search_dense` is therefore covered at the SQL expression level and
  by the nearest-neighbour check in the embed operation tests, not through the HTTP API.
- The HNSW index covers 1024 dimensions only.

## Verification

Run against an owned disposable PostgreSQL 16 database (pgvector image) at schema
head, with `backend/tests/retrieval/storage/test_vector_embeddings.py`:

- insert trigger populates the vector column; nearest-neighbour order is correct
  and another tenant's identical vector is never returned
- the planner uses the HNSW index for the 1024-dimension query
- non-1024 dimensions are stored without the index
- the search expression executes with threshold, order and limit
- downgrade to the previous revision removes the column; re-upgrade restores the index

Also run: `ruff check`/`ruff format`, `mypy` on `dense_search.py`, and
`backend/tests/phase5/unit` (all pass). Two tests in
`retrieval/storage/test_index_search.py` (memory queries) fail identically with and
without these changes.
