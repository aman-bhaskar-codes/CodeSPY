from unittest.mock import AsyncMock
from uuid import uuid4

import pytest


@pytest.fixture
def qualified_profile(monkeypatch):
    # These profile tests isolate unavailable-provider behavior, not publication authority.
    from app.contracts.indexing import IndexCoverageV1
    from app.services.retrieval_selection import ResolvedChunkSetV1

    selected = ResolvedChunkSetV1(
        uuid4(),
        uuid4(),
        "a" * 64,
        "b" * 64,
        IndexCoverageV1(admitted_files=0, selected_files=0, processed_files=0),
    )
    monkeypatch.setattr(
        "app.services.dense_search.resolve_lexical_set", AsyncMock(return_value=selected)
    )
    monkeypatch.setattr("app.services.dense_search.require_retrieval_source", AsyncMock())
