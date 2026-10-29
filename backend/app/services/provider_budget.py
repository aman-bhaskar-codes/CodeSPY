"""Provider disclosure policy enforcement and transaction-safe budget accounting."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.provider_budget import (
    BudgetReservationRequestV1,
    DisclosurePolicyV1,
    PriceScheduleV1,
    ReservationRecordV1,
    ReservationState,
    SettlementOutcomeV1,
    calculate_charge_micros,
)
from app.domain.budget_position import BudgetPosition
from app.models.budget_tables import (
    embedding_budgets,
    embedding_reservations,
    price_schedules,
)


class ProviderBudgetError(Exception):
    """Base error for provider budget operations."""


class DisclosureDeniedError(ProviderBudgetError):
    """External provider disclosure is not permitted under repository policy."""


class MissingPriceError(ProviderBudgetError):
    """No price schedule found for provider and model."""


class StalePriceError(ProviderBudgetError):
    """Price schedule for provider and model is expired."""


class BudgetExceededError(ProviderBudgetError):
    """Insufficient balance available for the requested operation."""


class ReservationNotFoundError(ProviderBudgetError):
    """Reservation record not found."""


class InvalidReservationTransitionError(ProviderBudgetError):
    """Invalid reservation state transition."""


class ProviderBudgetService:
    """Manages disclosure verification, price schedules, and row-locked reservations."""

    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def ensure_budget(
        self,
        org_id: UUID,
        repository_id: UUID,
        initial_balance_micros: int = 10_000_000,
        currency: str = "USD",
    ) -> None:
        """Seed or ensure budget record exists for an org repository."""
        stmt = select(embedding_budgets).where(
            embedding_budgets.c.org_id == org_id,
            embedding_budgets.c.repository_id == repository_id,
        )
        existing = (await self.s.execute(stmt)).mappings().one_or_none()
        if existing is None:
            await self.s.execute(
                embedding_budgets.insert().values(
                    org_id=org_id,
                    repository_id=repository_id,
                    currency=currency,
                    balance_micros=initial_balance_micros,
                    reserved_micros=0,
                    write_version=1,
                )
            )

    async def get_budget(self, org_id: UUID, repository_id: UUID) -> dict[str, int | str]:
        stmt = select(embedding_budgets).where(
            embedding_budgets.c.org_id == org_id,
            embedding_budgets.c.repository_id == repository_id,
        )
        row = (await self.s.execute(stmt)).mappings().one_or_none()
        if row is None:
            raise BudgetExceededError("Budget account does not exist for repository")
        return {
            "balance_micros": row["balance_micros"],
            "reserved_micros": row["reserved_micros"],
            "available_micros": BudgetPosition.from_row(row).available_micros,
            "debt_micros": row["debt_micros"],
            "currency": row["currency"],
            "write_version": row["write_version"],
        }

    async def set_price_schedule(self, schedule: PriceScheduleV1) -> None:
        stmt = select(price_schedules).where(
            price_schedules.c.provider == schedule.provider,
            price_schedules.c.model_id == schedule.model_id,
            price_schedules.c.version == schedule.version,
        )
        existing = (await self.s.execute(stmt)).mappings().one_or_none()
        if existing:
            await self.s.execute(
                price_schedules.update()
                .where(
                    price_schedules.c.provider == schedule.provider,
                    price_schedules.c.model_id == schedule.model_id,
                    price_schedules.c.version == schedule.version,
                )
                .values(
                    input_micros_per_1k=schedule.input_micros_per_1k,
                    effective_at=schedule.effective_at,
                    expires_at=schedule.expires_at,
                )
            )
        else:
            await self.s.execute(
                price_schedules.insert().values(
                    provider=schedule.provider,
                    model_id=schedule.model_id,
                    version=schedule.version,
                    input_micros_per_1k=schedule.input_micros_per_1k,
                    effective_at=schedule.effective_at,
                    expires_at=schedule.expires_at,
                )
            )

    async def get_active_price(
        self,
        provider: str,
        model_id: str,
        at: datetime | None = None,
    ) -> PriceScheduleV1:
        ts = at or datetime.now(UTC)
        stmt = (
            select(price_schedules)
            .where(
                price_schedules.c.provider == provider,
                price_schedules.c.model_id == model_id,
            )
            .order_by(price_schedules.c.effective_at.desc())
        )
        rows = (await self.s.execute(stmt)).mappings().all()
        if not rows:
            raise MissingPriceError(f"No price schedule found for {provider}:{model_id}")

        for r in rows:
            sched = PriceScheduleV1(
                provider=r["provider"],
                model_id=r["model_id"],
                version=r["version"],
                input_micros_per_1k=r["input_micros_per_1k"],
                effective_at=r["effective_at"],
                expires_at=r["expires_at"],
            )
            if sched.is_active(ts):
                return sched

        raise StalePriceError(
            f"All price schedules for {provider}:{model_id} are expired or "
            f"not yet effective at {ts}"
        )

    async def reserve(
        self,
        request: BudgetReservationRequestV1,
        policy: DisclosurePolicyV1,
        at: datetime | None = None,
    ) -> ReservationRecordV1:
        ts = at or datetime.now(UTC)

        # 1. Policy disclosure verification
        if not policy.is_provider_permitted(request.provider):
            raise DisclosureDeniedError(
                f"Provider {request.provider} is not permitted under repository disclosure policy"
            )

        if self.s.bind is not None and self.s.bind.dialect.name == "postgresql":
            from app.repositories.accounting_repository import AccountingRepository

            row = await AccountingRepository(self.s).reserve_embedding_request(request)
            return ReservationRecordV1.model_validate(
                {k: row[k] for k in ReservationRecordV1.model_fields}
            )

        # 2. Price schedule verification
        price = await self.get_active_price(request.provider, request.model_id, ts)
        estimated_charge = calculate_charge_micros(
            request.estimated_tokens, price.input_micros_per_1k
        )
        if estimated_charge > request.max_charge_micros:
            raise BudgetExceededError(
                f"Estimated charge {estimated_charge} micros exceeds caller max charge bound "
                f"{request.max_charge_micros} micros"
            )

        # 3. Idempotency check
        existing_res = (
            (
                await self.s.execute(
                    select(embedding_reservations).where(
                        embedding_reservations.c.org_id == request.org_id,
                        embedding_reservations.c.idempotency_key == request.idempotency_key,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )

        if existing_res is not None:
            return ReservationRecordV1(
                reservation_id=existing_res["reservation_id"],
                org_id=existing_res["org_id"],
                repository_id=existing_res["repository_id"],
                principal_id=existing_res["principal_id"],
                provider=existing_res["provider"],
                model_id=existing_res["model_id"],
                estimated_tokens=existing_res["estimated_tokens"],
                estimated_micros=existing_res["estimated_micros"],
                settled_micros=existing_res["settled_micros"],
                state=ReservationState(existing_res["state"]),
                idempotency_key=existing_res["idempotency_key"],
                created_at=existing_res["created_at"],
                updated_at=existing_res["updated_at"],
                failure_reason=existing_res["failure_reason"],
            )

        # 4. Atomic row-lock reservation on budget
        budget_row = (
            (
                await self.s.execute(
                    select(embedding_budgets)
                    .where(
                        embedding_budgets.c.org_id == request.org_id,
                        embedding_budgets.c.repository_id == request.repository_id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )

        if budget_row is None:
            raise BudgetExceededError("Budget account does not exist for repository")

        if budget_row["debt_micros"]:
            raise BudgetExceededError("Outstanding budget debt blocks new reservations")
        available = BudgetPosition.from_row(budget_row).available_micros
        if available < estimated_charge:
            raise BudgetExceededError(
                f"Insufficient available budget: available {available} micros < "
                f"needed {estimated_charge} micros"
            )

        # Update reserved hold
        await self.s.execute(
            embedding_budgets.update()
            .where(
                embedding_budgets.c.org_id == request.org_id,
                embedding_budgets.c.repository_id == request.repository_id,
            )
            .values(
                reserved_micros=budget_row["reserved_micros"] + estimated_charge,
                updated_at=ts,
                write_version=budget_row["write_version"] + 1,
            )
        )

        reservation_id = uuid4()
        await self.s.execute(
            embedding_reservations.insert().values(
                org_id=request.org_id,
                reservation_id=reservation_id,
                repository_id=request.repository_id,
                principal_id=request.principal_id,
                provider=request.provider,
                model_id=request.model_id,
                estimated_tokens=request.estimated_tokens,
                estimated_micros=estimated_charge,
                settled_micros=None,
                state=ReservationState.RESERVED.value,
                idempotency_key=request.idempotency_key,
                failure_reason=None,
                created_at=ts,
                updated_at=ts,
            )
        )

        return ReservationRecordV1(
            reservation_id=reservation_id,
            org_id=request.org_id,
            repository_id=request.repository_id,
            principal_id=request.principal_id,
            provider=request.provider,
            model_id=request.model_id,
            estimated_tokens=request.estimated_tokens,
            estimated_micros=estimated_charge,
            settled_micros=None,
            state=ReservationState.RESERVED,
            idempotency_key=request.idempotency_key,
            created_at=ts,
            updated_at=ts,
            failure_reason=None,
        )

    async def settle(
        self,
        org_id: UUID,
        outcome: SettlementOutcomeV1,
        at: datetime | None = None,
    ) -> ReservationRecordV1:
        ts = at or datetime.now(UTC)
        if self.s.bind is not None and self.s.bind.dialect.name == "postgresql":
            from app.repositories.accounting_repository import AccountingRepository

            row = await AccountingRepository(self.s).transition_embedding_request(org_id, outcome)
            return ReservationRecordV1.model_validate(
                {k: row[k] for k in ReservationRecordV1.model_fields}
            )

        res_row = (
            (
                await self.s.execute(
                    select(embedding_reservations)
                    .where(
                        embedding_reservations.c.org_id == org_id,
                        embedding_reservations.c.reservation_id == outcome.reservation_id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )

        if res_row is None:
            raise ReservationNotFoundError(f"Reservation {outcome.reservation_id} not found")

        current_state = ReservationState(res_row["state"])
        if current_state != ReservationState.RESERVED:
            raise InvalidReservationTransitionError(
                f"Cannot settle reservation in state {current_state}"
            )

        budget_row = (
            (
                await self.s.execute(
                    select(embedding_budgets)
                    .where(
                        embedding_budgets.c.org_id == org_id,
                        embedding_budgets.c.repository_id == res_row["repository_id"],
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )

        if budget_row is None:
            raise BudgetExceededError("Budget account does not exist")

        est_micros = res_row["estimated_micros"]

        if outcome.status == "success":
            actual_micros = outcome.actual_charge_micros
            position = BudgetPosition.from_row(budget_row).complete(est_micros, actual_micros)

            await self.s.execute(
                embedding_budgets.update()
                .where(
                    embedding_budgets.c.org_id == org_id,
                    embedding_budgets.c.repository_id == res_row["repository_id"],
                )
                .values(
                    **position.update_values(),
                    updated_at=ts,
                    write_version=budget_row["write_version"] + 1,
                )
            )

            await self.s.execute(
                embedding_reservations.update()
                .where(
                    embedding_reservations.c.org_id == org_id,
                    embedding_reservations.c.reservation_id == outcome.reservation_id,
                )
                .values(
                    state=ReservationState.COMMITTED.value,
                    settled_micros=actual_micros,
                    updated_at=ts,
                )
            )

            new_state = ReservationState.COMMITTED
            settled_val = actual_micros
            fail_msg = None

        elif outcome.status == "failed":
            position = BudgetPosition.from_row(budget_row).complete(est_micros)
            await self.s.execute(
                embedding_budgets.update()
                .where(
                    embedding_budgets.c.org_id == org_id,
                    embedding_budgets.c.repository_id == res_row["repository_id"],
                )
                .values(
                    **position.update_values(),
                    updated_at=ts,
                    write_version=budget_row["write_version"] + 1,
                )
            )

            await self.s.execute(
                embedding_reservations.update()
                .where(
                    embedding_reservations.c.org_id == org_id,
                    embedding_reservations.c.reservation_id == outcome.reservation_id,
                )
                .values(
                    state=ReservationState.RELEASED.value,
                    failure_reason=outcome.error_message or "dispatch_failed",
                    updated_at=ts,
                )
            )

            new_state = ReservationState.RELEASED
            settled_val = None
            fail_msg = outcome.error_message or "dispatch_failed"

        elif outcome.status == "timeout":
            # Hold uncertain: reservation remains active hold on budget, not released
            await self.s.execute(
                embedding_reservations.update()
                .where(
                    embedding_reservations.c.org_id == org_id,
                    embedding_reservations.c.reservation_id == outcome.reservation_id,
                )
                .values(
                    state=ReservationState.UNCERTAIN_HOLD.value,
                    failure_reason="timeout_during_dispatch",
                    updated_at=ts,
                )
            )

            new_state = ReservationState.UNCERTAIN_HOLD
            settled_val = None
            fail_msg = "timeout_during_dispatch"

        else:
            raise InvalidReservationTransitionError(
                f"Unsupported settlement status: {outcome.status}"
            )

        return ReservationRecordV1(
            reservation_id=outcome.reservation_id,
            org_id=org_id,
            repository_id=res_row["repository_id"],
            principal_id=res_row["principal_id"],
            provider=res_row["provider"],
            model_id=res_row["model_id"],
            estimated_tokens=res_row["estimated_tokens"],
            estimated_micros=res_row["estimated_micros"],
            settled_micros=settled_val,
            state=new_state,
            idempotency_key=res_row["idempotency_key"],
            created_at=res_row["created_at"],
            updated_at=ts,
            failure_reason=fail_msg,
        )

    async def mark_dispatched(self, org_id: UUID, reservation_id: UUID) -> bool:
        """Commit the returned claim before I/O; false forbids another dispatch."""
        if self.s.bind is None or self.s.bind.dialect.name != "postgresql":
            raise InvalidReservationTransitionError("durable dispatch requires PostgreSQL")
        from app.repositories.accounting_repository import AccountingRepository

        return await AccountingRepository(self.s).claim_embedding_dispatch(org_id, reservation_id)

    async def cancel(
        self,
        org_id: UUID,
        reservation_id: UUID,
        reason: str = "cancelled_by_caller",
        at: datetime | None = None,
    ) -> ReservationRecordV1:
        ts = at or datetime.now(UTC)
        if self.s.bind is not None and self.s.bind.dialect.name == "postgresql":
            from app.repositories.accounting_repository import AccountingRepository

            row = await AccountingRepository(self.s).cancel_embedding_request(
                org_id, reservation_id, reason
            )
            return ReservationRecordV1.model_validate(
                {k: row[k] for k in ReservationRecordV1.model_fields}
            )

        res_row = (
            (
                await self.s.execute(
                    select(embedding_reservations)
                    .where(
                        embedding_reservations.c.org_id == org_id,
                        embedding_reservations.c.reservation_id == reservation_id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )

        if res_row is None:
            raise ReservationNotFoundError(f"Reservation {reservation_id} not found")

        current_state = ReservationState(res_row["state"])
        if current_state != ReservationState.RESERVED:
            raise InvalidReservationTransitionError(
                f"Cannot cancel reservation in state {current_state}"
            )

        budget_row = (
            (
                await self.s.execute(
                    select(embedding_budgets)
                    .where(
                        embedding_budgets.c.org_id == org_id,
                        embedding_budgets.c.repository_id == res_row["repository_id"],
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )

        if budget_row is not None:
            position = BudgetPosition.from_row(budget_row).complete(res_row["estimated_micros"])
            await self.s.execute(
                embedding_budgets.update()
                .where(
                    embedding_budgets.c.org_id == org_id,
                    embedding_budgets.c.repository_id == res_row["repository_id"],
                )
                .values(
                    **position.update_values(),
                    updated_at=ts,
                    write_version=budget_row["write_version"] + 1,
                )
            )

        await self.s.execute(
            embedding_reservations.update()
            .where(
                embedding_reservations.c.org_id == org_id,
                embedding_reservations.c.reservation_id == reservation_id,
            )
            .values(
                state=ReservationState.RELEASED.value,
                failure_reason=reason,
                updated_at=ts,
            )
        )

        return ReservationRecordV1(
            reservation_id=reservation_id,
            org_id=org_id,
            repository_id=res_row["repository_id"],
            principal_id=res_row["principal_id"],
            provider=res_row["provider"],
            model_id=res_row["model_id"],
            estimated_tokens=res_row["estimated_tokens"],
            estimated_micros=res_row["estimated_micros"],
            settled_micros=None,
            state=ReservationState.RELEASED,
            idempotency_key=res_row["idempotency_key"],
            created_at=res_row["created_at"],
            updated_at=ts,
            failure_reason=reason,
        )
