"""Job projections must return one coherent database statement snapshot."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from app.repositories.job_repository import load_job


@pytest.mark.asyncio
async def test_index_projection_refreshes_state_with_publication_identity():
    org, job, index = uuid4(), uuid4(), uuid4()
    before = {"org_id": org, "id": job, "operation": "index_snapshot", "state": "running"}
    after = {**before, "state": "succeeded", "result_index_id": index}
    initial, projected = MagicMock(), MagicMock()
    initial.mappings.return_value.one_or_none.return_value = before
    projected.mappings.return_value.one.return_value = after
    session = SimpleNamespace(execute=AsyncMock(side_effect=[initial, projected]))
    actual = await load_job(session, org, job)
    assert actual["state"] == "succeeded"
    assert actual["result_index_id"] == index
    query = session.execute.call_args_list[1].args[0]
    assert "state" in query.selected_columns
    assert "finished_at" in query.selected_columns
