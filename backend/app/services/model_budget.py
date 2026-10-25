"""Transaction-safe model gateway budget reservations, price schedules, and financial ledger."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.model import GenerationPriceScheduleV1
from app.db.time import database_now
from app.models.budget_tables import embedding_budgets, price_schedules
from app.models.model_gateway_tables import model_reservations, model_spend_ledger


class ModelBudgetError(Exception):
    """Base error for model gateway budget operations."""


class ModelBudgetExceededError(ModelBudgetError):
    """Insufficient balance available for model generation operation."""


class ModelMissingPriceError(ModelBudgetError):
    """No active generation price schedule found."""


class ModelStalePriceError(ModelBudgetError):
    """Price schedule for generation model is expired."""


class ModelReservationNotFoundError(ModelBudgetError):
    """Model reservation record not found."""


class ModelInvalidStateTransitionError(ModelBudgetError):
    """Invalid state transition for model reservation."""


class ModelBudgetService:
    """Manages multi-tenant budget row-locking, generation reservations, and spend audit."""

    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def ensure_budget(
        self,
        org_id: UUID,
        repository_id: UUID,
        initial_balance_micros: int = 10_000_000,
        currency: str = "USD",
    ) -> None:
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

    async def get_budget(self, org_id: UUID, repository_id: UUID) -> dict[str, Any]:
        stmt = select(embedding_budgets).where(
            embedding_budgets.c.org_id == org_id,
            embedding_budgets.c.repository_id == repository_id,
        )
        row = (await self.s.execute(stmt)).mappings().one_or_none()
        if row is None:
            raise ModelBudgetExceededError("Budget account does not exist for repository")
        return {
            "balance_micros": row["balance_micros"],
            "reserved_micros": row["reserved_micros"],
            "available_micros": row["balance_micros"] - row["reserved_micros"],
            "currency": row["currency"],
            "write_version": row["write_version"],
        }

    async def set_generation_price_schedule(self, schedule: GenerationPriceScheduleV1) -> None:
        eff = (
            schedule.effective_at
            if schedule.effective_at.tzinfo is not None
            else schedule.effective_at.replace(tzinfo=UTC)
        )
        exp = (
            schedule.expires_at
            if schedule.expires_at.tzinfo is not None
            else schedule.expires_at.replace(tzinfo=UTC)
        )
        stmt = select(price_schedules).where(
            price_schedules.c.provider == schedule.provider,
            price_schedules.c.model_id == schedule.model_id,
            price_schedules.c.version == schedule.version,
        )
        existing = (await self.s.execute(stmt)).mappings().one_or_none()
        vals = {
            "input_micros_per_1k": schedule.input_micros_per_1k,
            "output_micros_per_1k": schedule.output_micros_per_1k,
            "thinking_micros_per_1k": schedule.thinking_micros_per_1k,
            "cache_read_micros_per_1k": schedule.cache_read_micros_per_1k,
            "cache_write_micros_per_1k": schedule.cache_write_micros_per_1k,
            "effective_at": eff,
            "expires_at": exp,
        }
        if existing is None:
            await self.s.execute(
                price_schedules.insert().values(
                    provider=schedule.provider,
                    model_id=schedule.model_id,
                    version=schedule.version,
                    **vals,
                )
            )
        else:
            for key, value in vals.items():
                stored = existing[key]
                if isinstance(stored, datetime) and stored.tzinfo is None:
                    stored = stored.replace(tzinfo=UTC)
                if stored != value:
                    raise ModelInvalidStateTransitionError("Price version is immutable")

    async def ensure_default_generation_pricing(self) -> None:
        """Ensures a baseline active price schedule exists for local deterministic fake models."""
        try:
            await self.get_generation_price_schedule("fake", "codespy-fake-investigator-v1")
        except Exception:
            now = datetime.now(UTC)
            await self.set_generation_price_schedule(
                GenerationPriceScheduleV1(
                    provider="fake",
                    model_id="codespy-fake-investigator-v1",
                    version="2026-10-22.1",
                    input_micros_per_1k=500,
                    output_micros_per_1k=1000,
                    effective_at=now - timedelta(days=1),
                    expires_at=now + timedelta(days=365),
                )
            )

    async def get_generation_price_schedule(
        self, provider: str, model_id: str, version: str | None = None
    ) -> GenerationPriceScheduleV1:
        conditions = [
            price_schedules.c.provider == provider,
            price_schedules.c.model_id == model_id,
        ]
        if version:
            conditions.append(price_schedules.c.version == version)
        stmt = (
            select(price_schedules)
            .where(*conditions)
            .order_by(price_schedules.c.effective_at.desc())
        )
        row = (await self.s.execute(stmt)).mappings().first()
        if not row:
            raise ModelMissingPriceError(f"No price schedule found for {provider}/{model_id}")
        schedule = GenerationPriceScheduleV1(
            provider=row["provider"],
            model_id=row["model_id"],
            version=row["version"],
            input_micros_per_1k=row["input_micros_per_1k"],
            output_micros_per_1k=row.get("output_micros_per_1k") or 0,
            thinking_micros_per_1k=row.get("thinking_micros_per_1k") or 0,
            cache_read_micros_per_1k=row.get("cache_read_micros_per_1k") or 0,
            cache_write_micros_per_1k=row.get("cache_write_micros_per_1k") or 0,
            effective_at=row["effective_at"].replace(tzinfo=UTC)
            if row["effective_at"].tzinfo is None
            else row["effective_at"],
            expires_at=row["expires_at"].replace(tzinfo=UTC)
            if row["expires_at"].tzinfo is None
            else row["expires_at"],
        )
        if not schedule.is_active():
            raise ModelStalePriceError(f"Price schedule {schedule.version} is expired")
        return schedule

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
        if (
            min(estimated_input_tokens, estimated_output_tokens, estimated_micros) < 0
            or attempt_id < 1
        ):
            raise ModelInvalidStateTransitionError("Invalid reservation bounds")
        if (request_digest is None) != (price_snapshot is None):
            raise ModelInvalidStateTransitionError("Incomplete model request identity")
        if request_digest is not None and (
            len(request_digest) != 64 or any(c not in "0123456789abcdef" for c in request_digest)
        ):
            raise ModelInvalidStateTransitionError("Invalid model request digest")
        if price_snapshot is not None and (
            price_snapshot.provider != provider or price_snapshot.model_id != model_id
        ):
            raise ModelInvalidStateTransitionError("Price snapshot scope conflicts")
        price_json = price_snapshot.model_dump(mode="json") if price_snapshot else None
        binding = {
            "request_digest": request_digest,
            "price_snapshot": price_json,
            "repository_id": repository_id,
            "principal_id": principal_id,
            "investigation_id": investigation_id,
            "logical_call_id": logical_call_id,
            "attempt_id": attempt_id,
            "provider": provider,
            "model_id": model_id,
            "charge_category": charge_category,
            "estimated_input_tokens": estimated_input_tokens,
            "estimated_output_tokens": estimated_output_tokens,
            "reserved_micros": estimated_micros,
        }

        def replay(row: Any) -> UUID:
            if any(row[key] != value for key, value in binding.items()):
                raise ModelInvalidStateTransitionError("Reservation request key conflicts")
            return UUID(str(row["reservation_id"]))

        stmt_idem = select(model_reservations).where(
            model_reservations.c.org_id == org_id,
            model_reservations.c.idempotency_key == idempotency_key,
        )
        existing = (await self.s.execute(stmt_idem)).mappings().one_or_none()
        if existing:
            return replay(existing)

        # Lock budget row
        stmt_budget = (
            select(embedding_budgets)
            .where(
                embedding_budgets.c.org_id == org_id,
                embedding_budgets.c.repository_id == repository_id,
            )
            .with_for_update()
        )
        budget = (await self.s.execute(stmt_budget)).mappings().one_or_none()
        if not budget:
            raise ModelBudgetExceededError("Budget account does not exist for repository")

        # A concurrent request can commit while this transaction waits for the budget lock.
        existing = (await self.s.execute(stmt_idem)).mappings().one_or_none()
        if existing:
            return replay(existing)

        available = budget["balance_micros"] - budget["reserved_micros"]
        if available < estimated_micros:
            raise ModelBudgetExceededError(
                f"Insufficient funds available ({available} < {estimated_micros} micros)"
            )

        # Update budget reserved
        await self.s.execute(
            update(embedding_budgets)
            .where(
                embedding_budgets.c.org_id == org_id,
                embedding_budgets.c.repository_id == repository_id,
            )
            .values(
                reserved_micros=budget["reserved_micros"] + estimated_micros,
                write_version=budget["write_version"] + 1,
                updated_at=datetime.now(UTC),
            )
        )

        reservation_id = uuid4()
        now = datetime.now(UTC)
        await self.s.execute(
            model_reservations.insert().values(
                org_id=org_id,
                reservation_id=reservation_id,
                repository_id=repository_id,
                principal_id=principal_id,
                investigation_id=investigation_id,
                logical_call_id=logical_call_id,
                attempt_id=attempt_id,
                provider=provider,
                model_id=model_id,
                charge_category=charge_category,
                estimated_input_tokens=estimated_input_tokens,
                estimated_output_tokens=estimated_output_tokens,
                reserved_micros=estimated_micros,
                settled_micros=None,
                request_digest=request_digest,
                price_snapshot=price_json,
                state="reserved",
                idempotency_key=idempotency_key,
                created_at=now,
                updated_at=now,
            )
        )
        return reservation_id

    async def get_reserved_price(
        self, org_id: UUID, reservation_id: UUID
    ) -> GenerationPriceScheduleV1:
        row = (
            (
                await self.s.execute(
                    select(model_reservations).where(
                        model_reservations.c.org_id == org_id,
                        model_reservations.c.reservation_id == reservation_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ModelReservationNotFoundError("Model reservation unavailable")
        if row["request_digest"] is None or row["price_snapshot"] is None:
            raise ModelInvalidStateTransitionError("Legacy reservation has no captured price")
        price = GenerationPriceScheduleV1.model_validate(row["price_snapshot"])
        if price.provider != row["provider"] or price.model_id != row["model_id"]:
            raise ModelInvalidStateTransitionError("Captured price scope conflicts")
        return price

    async def commit_dispatch_intent(self, org_id: UUID, reservation_id: UUID) -> None:
        stmt = (
            update(model_reservations)
            .where(
                model_reservations.c.org_id == org_id,
                model_reservations.c.reservation_id == reservation_id,
                model_reservations.c.state == "reserved",
            )
            .values(state="dispatch_intent_committed", updated_at=datetime.now(UTC))
        )
        res = await self.s.execute(stmt)
        rowcount = getattr(res, "rowcount", -1)
        if rowcount == 0:
            raise ModelInvalidStateTransitionError(
                f"Cannot commit dispatch intent for reservation {reservation_id}"
            )

    async def mark_dispatched(self, org_id: UUID, reservation_id: UUID) -> None:
        stmt = (
            update(model_reservations)
            .where(
                model_reservations.c.org_id == org_id,
                model_reservations.c.reservation_id == reservation_id,
                model_reservations.c.state == "dispatch_intent_committed",
            )
            .values(state="dispatched", updated_at=datetime.now(UTC))
        )
        res = await self.s.execute(stmt)
        rowcount = getattr(res, "rowcount", -1)
        if rowcount == 0:
            raise ModelInvalidStateTransitionError(
                f"Cannot mark dispatched for reservation {reservation_id}"
            )

    async def settle_reservation(
        self,
        org_id: UUID,
        reservation_id: UUID,
        actual_input_tokens: int,
        actual_output_tokens: int,
        actual_thinking_tokens: int,
        actual_micros: int,
        provider_request_id: str | None = None,
    ) -> None:
        stmt_res = (
            select(model_reservations)
            .where(
                model_reservations.c.org_id == org_id,
                model_reservations.c.reservation_id == reservation_id,
            )
            .with_for_update()
        )
        res_row = (await self.s.execute(stmt_res)).mappings().one_or_none()
        if not res_row:
            raise ModelReservationNotFoundError(f"Reservation {reservation_id} not found")

        if res_row["state"] == "settled":
            ledger = (
                (
                    await self.s.execute(
                        select(model_spend_ledger).where(
                            model_spend_ledger.c.org_id == org_id,
                            model_spend_ledger.c.reservation_id == reservation_id,
                            model_spend_ledger.c.is_uncertain_hold.is_(False),
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if (
                ledger is None
                or res_row["settled_micros"] != actual_micros
                or res_row["provider_request_id"] != provider_request_id
                or ledger["input_tokens"] != actual_input_tokens
                or ledger["output_tokens"] != actual_output_tokens
                or ledger["thinking_tokens"] != actual_thinking_tokens
            ):
                raise ModelInvalidStateTransitionError(
                    "Settlement replay conflicts with recorded usage"
                )
            return
        if res_row["state"] not in ("dispatch_intent_committed", "dispatched", "uncertain_hold"):
            raise ModelInvalidStateTransitionError("Reservation has no dispatch intent")
        if (
            min(actual_input_tokens, actual_output_tokens, actual_thinking_tokens, actual_micros)
            < 0
        ):
            raise ModelInvalidStateTransitionError("Invalid settlement usage")

        repo_id = res_row["repository_id"]
        reserved_micros = res_row["reserved_micros"]

        # Lock and update budget balance
        stmt_budget = (
            select(embedding_budgets)
            .where(
                embedding_budgets.c.org_id == org_id,
                embedding_budgets.c.repository_id == repo_id,
            )
            .with_for_update()
        )
        b_row = (await self.s.execute(stmt_budget)).mappings().one()

        new_balance = max(0, b_row["balance_micros"] - actual_micros)
        new_reserved = max(0, b_row["reserved_micros"] - reserved_micros)

        await self.s.execute(
            update(embedding_budgets)
            .where(
                embedding_budgets.c.org_id == org_id,
                embedding_budgets.c.repository_id == repo_id,
            )
            .values(
                balance_micros=new_balance,
                reserved_micros=new_reserved,
                write_version=b_row["write_version"] + 1,
                updated_at=datetime.now(UTC),
            )
        )

        now = datetime.now(UTC)
        await self.s.execute(
            update(model_reservations)
            .where(
                model_reservations.c.org_id == org_id,
                model_reservations.c.reservation_id == reservation_id,
            )
            .values(
                state="settled",
                settled_micros=actual_micros,
                provider_request_id=provider_request_id,
                updated_at=now,
            )
        )

        # Insert audit record in spend ledger
        await self.s.execute(
            model_spend_ledger.insert().values(
                org_id=org_id,
                id=uuid4(),
                repository_id=repo_id,
                reservation_id=reservation_id,
                investigation_id=res_row["investigation_id"],
                attempt_id=res_row["attempt_id"],
                provider=res_row["provider"],
                model_id=res_row["model_id"],
                charge_category=res_row["charge_category"],
                input_tokens=actual_input_tokens,
                output_tokens=actual_output_tokens,
                thinking_tokens=actual_thinking_tokens,
                charged_micros=actual_micros,
                is_uncertain_hold=False,
                created_at=now,
            )
        )

    async def hold_uncertain(
        self,
        org_id: UUID,
        reservation_id: UUID,
        failure_reason: str = "timeout_during_dispatch",
    ) -> None:
        stmt_res = (
            select(model_reservations)
            .where(
                model_reservations.c.org_id == org_id,
                model_reservations.c.reservation_id == reservation_id,
            )
            .with_for_update()
        )
        res_row = (await self.s.execute(stmt_res)).mappings().one_or_none()
        if not res_row:
            raise ModelReservationNotFoundError(f"Reservation {reservation_id} not found")

        if res_row["state"] in (
            "uncertain_hold",
            "settled",
            "rejected_before_dispatch",
            "conservatively_reconciled",
        ):
            return
        if res_row["state"] not in ("dispatch_intent_committed", "dispatched"):
            raise ModelInvalidStateTransitionError("Reservation has no dispatch intent")

        now = datetime.now(UTC)
        await self.s.execute(
            update(model_reservations)
            .where(
                model_reservations.c.org_id == org_id,
                model_reservations.c.reservation_id == reservation_id,
            )
            .values(
                state="uncertain_hold",
                failure_reason=failure_reason,
                updated_at=now,
            )
        )

        # Audit uncertain hold in ledger
        await self.s.execute(
            model_spend_ledger.insert().values(
                org_id=org_id,
                id=uuid4(),
                repository_id=res_row["repository_id"],
                reservation_id=reservation_id,
                investigation_id=res_row["investigation_id"],
                attempt_id=res_row["attempt_id"],
                provider=res_row["provider"],
                model_id=res_row["model_id"],
                charge_category=res_row["charge_category"],
                input_tokens=res_row["estimated_input_tokens"],
                output_tokens=res_row["estimated_output_tokens"],
                thinking_tokens=0,
                charged_micros=res_row["reserved_micros"],
                is_uncertain_hold=True,
                created_at=now,
            )
        )

    async def release_reservation(
        self,
        org_id: UUID,
        reservation_id: UUID,
        failure_reason: str = "cancelled_before_dispatch",
    ) -> None:
        stmt_res = (
            select(model_reservations)
            .where(
                model_reservations.c.org_id == org_id,
                model_reservations.c.reservation_id == reservation_id,
            )
            .with_for_update()
        )
        res_row = (await self.s.execute(stmt_res)).mappings().one_or_none()
        if not res_row:
            raise ModelReservationNotFoundError(f"Reservation {reservation_id} not found")

        if res_row["state"] in ("settled", "rejected_before_dispatch"):
            return
        if res_row["state"] != "reserved":
            raise ModelInvalidStateTransitionError("Cannot release potentially spent capacity")

        repo_id = res_row["repository_id"]
        reserved_micros = res_row["reserved_micros"]

        stmt_budget = (
            select(embedding_budgets)
            .where(
                embedding_budgets.c.org_id == org_id,
                embedding_budgets.c.repository_id == repo_id,
            )
            .with_for_update()
        )
        b_row = (await self.s.execute(stmt_budget)).mappings().one()
        new_reserved = max(0, b_row["reserved_micros"] - reserved_micros)

        await self.s.execute(
            update(embedding_budgets)
            .where(
                embedding_budgets.c.org_id == org_id,
                embedding_budgets.c.repository_id == repo_id,
            )
            .values(
                reserved_micros=new_reserved,
                write_version=b_row["write_version"] + 1,
                updated_at=datetime.now(UTC),
            )
        )

        await self.s.execute(
            update(model_reservations)
            .where(
                model_reservations.c.org_id == org_id,
                model_reservations.c.reservation_id == reservation_id,
            )
            .values(
                state="rejected_before_dispatch",
                failure_reason=failure_reason,
                updated_at=datetime.now(UTC),
            )
        )

    async def recover_dispatch_intents(
        self,
        org_id: UUID,
        repository_id: UUID,
        *,
        minimum_age_seconds: int = 300,
        limit: int = 100,
    ) -> tuple[UUID, ...]:
        """Conservatively hold bounded old dispatches; never replay or release them."""
        if minimum_age_seconds < 60 or not 1 <= limit <= 100:
            raise ValueError("Invalid dispatch recovery bounds")
        cutoff = await database_now(self.s) - timedelta(seconds=minimum_age_seconds)
        rows = (
            (
                await self.s.execute(
                    select(model_reservations.c.reservation_id)
                    .where(
                        model_reservations.c.org_id == org_id,
                        model_reservations.c.repository_id == repository_id,
                        model_reservations.c.state.in_(("dispatch_intent_committed", "dispatched")),
                        model_reservations.c.updated_at < cutoff,
                    )
                    .order_by(model_reservations.c.updated_at, model_reservations.c.reservation_id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        for identity in rows:
            await self.hold_uncertain(org_id, identity, failure_reason="abandoned_dispatch")
        return tuple(rows)
