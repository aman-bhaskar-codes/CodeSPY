"""Exact-second timestamps must have identical JSON and canonical SQL representations."""

from types import SimpleNamespace
from uuid import UUID

from app.domain.canonical_json import load_json
from app.repositories.scan_repository import ScanRepository
from tests.phase4.dependency_fixtures import NOW
from tests.phase4.unit.test_scan_config import config


async def test_attempt_json_uses_canonical_timestamp_representation(tmp_path):
    value, spec = config(tmp_path)
    rows = []

    class Session:
        async def execute(self, statement):
            rows.append(statement.compile().params)

    lease = SimpleNamespace(
        run_ref=spec.scope,
        org_id=spec.scope.org_id,
        job_id=UUID(int=101),
        attempt_id=UUID(int=102),
        fence=1,
    )
    await ScanRepository(Session()).create_attempt_spec(
        lease, UUID(int=103), spec.repository_id, value, NOW.replace(microsecond=0)
    )
    assert rows[0]["publication_spec_json"] == load_json(rows[0]["canonical_text"])
