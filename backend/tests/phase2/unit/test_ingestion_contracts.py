import asyncio
from uuid import uuid4

import pytest
from app.contracts.ingestion import AdmissionPolicyV1, UploadReservationV1
from app.services.finalization_budget import FinalizationBudget
from pydantic import ValidationError


def test_wire_contracts_reject_authority_and_invalid_limits():
    raw = (
        '{"schema_version":1,"repository_id":"'
        + str(uuid4())
        + '","filename":"fixture.zip","declared_size":10}'
    )
    assert UploadReservationV1.model_validate_json(raw).declared_size == 10
    with pytest.raises(ValidationError):
        UploadReservationV1.model_validate_json(raw[:-1] + ',"role":"owner"}')
    with pytest.raises(ValidationError):
        UploadReservationV1(repository_id=uuid4(), filename="a", declared_size=0)
    assert len(AdmissionPolicyV1().digest()) == 64


async def test_total_budget_covers_multiple_individually_short_waits():
    completed = []

    async def work():
        for i in range(4):
            await asyncio.sleep(0.03)
            completed.append(i)

    with pytest.raises(TimeoutError):
        async with FinalizationBudget(0.075):
            await work()
    assert len(completed) < 4


async def test_cleanup_invalidation_has_one_total_deadline():
    import asyncio

    from app.services.finalization_budget import rollback_or_invalidate

    calls = []

    class StuckSession:
        async def rollback(self):
            await asyncio.Event().wait()

        async def close(self):
            calls.append("closed")

        async def invalidate(self):
            calls.append("invalidating")
            await asyncio.Event().wait()

    with pytest.raises(TimeoutError):
        await rollback_or_invalidate(
            StuckSession(), seconds=0.05, force_terminate=lambda: calls.append("terminated")
        )
    assert calls == ["invalidating", "terminated"]
