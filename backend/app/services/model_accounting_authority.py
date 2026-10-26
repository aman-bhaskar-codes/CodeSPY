"""Generation service adapter using constrained financial mutation routines."""

from uuid import UUID, uuid4

from app.contracts.model import GenerationPriceScheduleV1
from app.repositories.accounting_repository import AccountingRepository
from app.services.model_budget import ModelBudgetService


class AuthorityModelBudgetService(ModelBudgetService):
    async def reserve_generation_budget(
        self,
        org_id: UUID,
        repository_id: UUID,
        principal_id: UUID,
        investigation_id: UUID,
        logical_call_id: UUID,
        attempt_id: int,
        provider: str,
        model_id: str,
        estimated_input_tokens: int,
        estimated_output_tokens: int,
        estimated_micros: int,
        idempotency_key: str,
        charge_category: str = "generation",
        request_digest: str | None = None,
        price_snapshot: GenerationPriceScheduleV1 | None = None,
    ) -> UUID:
        return await AccountingRepository(self.s).reserve_generation(
            org_id,
            uuid4(),
            repository_id,
            principal_id,
            investigation_id,
            logical_call_id,
            attempt_id,
            provider,
            model_id,
            charge_category,
            estimated_input_tokens,
            estimated_output_tokens,
            estimated_micros,
            idempotency_key,
            request_digest,
            price_snapshot,
        )

    async def commit_dispatch_intent(self, org_id: UUID, reservation_id: UUID) -> None:
        await AccountingRepository(self.s).commit_dispatch_intent(org_id, reservation_id)

    async def mark_dispatched(self, org_id: UUID, reservation_id: UUID) -> None:
        await AccountingRepository(self.s).mark_dispatched(org_id, reservation_id)

    async def settle_reservation(
        self,
        org_id: UUID,
        reservation_id: UUID,
        actual_input_tokens: int,
        actual_output_tokens: int,
        actual_thinking_tokens: int,
        actual_micros: int,
        provider_request_id: str | None = None,
        *,
        actual_cache_read_tokens: int = 0,
        actual_cache_write_tokens: int = 0,
    ) -> None:
        await AccountingRepository(self.s).settle_generation(
            org_id,
            reservation_id,
            actual_input_tokens,
            actual_output_tokens,
            actual_thinking_tokens,
            actual_micros,
            provider_request_id,
            actual_cache_read_tokens,
            actual_cache_write_tokens,
        )

    async def hold_uncertain(
        self, org_id: UUID, reservation_id: UUID, failure_reason: str = "timeout_during_dispatch"
    ) -> None:
        await AccountingRepository(self.s).hold_uncertain(org_id, reservation_id, failure_reason)

    async def release_reservation(
        self, org_id: UUID, reservation_id: UUID, failure_reason: str = "cancelled_before_dispatch"
    ) -> None:
        await AccountingRepository(self.s).release_generation(
            org_id, reservation_id, failure_reason
        )
