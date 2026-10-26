"""Constrained database operations adapter for least-privilege accounting."""

import json
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.model import GenerationPriceScheduleV1
from app.contracts.provider_budget import BudgetReservationRequestV1, SettlementOutcomeV1
from app.db.time import database_now
from app.domain.budget_position import BudgetPosition
from app.domain.errors import DuplicateConflict
from app.models.budget_tables import (
    budget_funding_receipts,
    embedding_budgets,
)
from app.models.model_gateway_tables import model_reservations, model_spend_ledger


class AccountingRepository:
    """Adapts accounting mutations to either PostgreSQL security-definer routines
    or direct transaction statements on SQLite / fallback dialects.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    def _is_postgres(self) -> bool:
        bind = self.s.bind
        return bool(bind and bind.dialect.name == "postgresql")

    async def reserve_generation(
        self,
        org_id: UUID,
        reservation_id: UUID,
        repository_id: UUID,
        principal_id: UUID,
        investigation_id: UUID,
        logical_call_id: UUID,
        attempt_id: int,
        provider: str,
        model_id: str,
        charge_category: str,
        estimated_input_tokens: int,
        estimated_output_tokens: int,
        reserved_micros: int,
        idempotency_key: str,
        request_digest: str | None = None,
        price_snapshot: GenerationPriceScheduleV1 | None = None,
    ) -> UUID:
        price_json = price_snapshot.model_dump(mode="json") if price_snapshot else None
        if self._is_postgres():
            try:
                stmt = text(
                    "SELECT public.codespy_reserve_generation("
                    ":p_org_id, :p_reservation_id, :p_repository_id, :p_principal_id, "
                    ":p_investigation_id, :p_logical_call_id, :p_attempt_id, :p_provider, "
                    ":p_model_id, :p_charge_category, :p_estimated_input_tokens, "
                    ":p_estimated_output_tokens, :p_reserved_micros, :p_idempotency_key, "
                    ":p_request_digest, CAST(:p_price_snapshot AS jsonb))"
                )
                res = await self.s.execute(
                    stmt,
                    {
                        "p_org_id": org_id,
                        "p_reservation_id": reservation_id,
                        "p_repository_id": repository_id,
                        "p_principal_id": principal_id,
                        "p_investigation_id": investigation_id,
                        "p_logical_call_id": logical_call_id,
                        "p_attempt_id": attempt_id,
                        "p_provider": provider,
                        "p_model_id": model_id,
                        "p_charge_category": charge_category,
                        "p_estimated_input_tokens": estimated_input_tokens,
                        "p_estimated_output_tokens": estimated_output_tokens,
                        "p_reserved_micros": reserved_micros,
                        "p_idempotency_key": idempotency_key,
                        "p_request_digest": request_digest,
                        "p_price_snapshot": json.dumps(price_json) if price_json else None,
                    },
                )
                return UUID(str(res.scalar_one()))
            except DBAPIError as exc:
                self._map_error(exc)

        # Fallback / SQLite implementation
        stmt_idem = select(model_reservations).where(
            model_reservations.c.org_id == org_id,
            model_reservations.c.idempotency_key == idempotency_key,
        )
        existing = (await self.s.execute(stmt_idem)).mappings().one_or_none()
        if existing:
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
                "reserved_micros": reserved_micros,
            }
            if any(existing[k] != v for k, v in binding.items()):
                from app.services.model_budget import ModelInvalidStateTransitionError

                raise ModelInvalidStateTransitionError("Reservation request key conflicts")
            return UUID(str(existing["reservation_id"]))

        stmt_b = (
            select(embedding_budgets)
            .where(
                embedding_budgets.c.org_id == org_id,
                embedding_budgets.c.repository_id == repository_id,
            )
            .with_for_update()
        )
        b_row = (await self.s.execute(stmt_b)).mappings().one_or_none()
        if not b_row:
            from app.services.model_budget import ModelBudgetExceededError

            raise ModelBudgetExceededError("Budget account does not exist for repository")

        pos = BudgetPosition.from_row(b_row)
        if pos.debt_micros > 0 or pos.available_micros < reserved_micros:
            from app.services.model_budget import ModelBudgetExceededError

            raise ModelBudgetExceededError("Insufficient spendable balance or active debt")

        if investigation_id is not None:
            from app.models.investigation_tables import investigations

            inv_limit = await self.s.scalar(
                select(investigations.c.budget_limit_micros).where(
                    investigations.c.org_id == org_id,
                    investigations.c.id == investigation_id,
                )
            )
            if inv_limit is not None and inv_limit > 0:
                res_rows = (
                    (
                        await self.s.execute(
                            select(
                                model_reservations.c.state,
                                model_reservations.c.reserved_micros,
                                model_reservations.c.settled_micros,
                            ).where(
                                model_reservations.c.org_id == org_id,
                                model_reservations.c.investigation_id == investigation_id,
                            )
                        )
                    )
                    .mappings()
                    .all()
                )

                committed_micros = 0
                for r in res_rows:
                    st = r["state"]
                    if st == "settled":
                        committed_micros += r["settled_micros"] or 0
                    elif st in (
                        "reserved",
                        "dispatch_intent_committed",
                        "dispatched",
                        "uncertain_hold",
                    ):
                        committed_micros += r["reserved_micros"] or 0

                if committed_micros + reserved_micros > inv_limit:
                    from app.services.model_budget import ModelBudgetExceededError

                    raise ModelBudgetExceededError(
                        f"Investigation budget limit exceeded: committed {committed_micros} + "
                        f"quote {reserved_micros} > limit {inv_limit}"
                    )

        new_pos = BudgetPosition(
            pos.balance_micros, pos.reserved_micros + reserved_micros, pos.debt_micros
        )
        now = await database_now(self.s)
        await self.s.execute(
            update(embedding_budgets)
            .where(
                embedding_budgets.c.org_id == org_id,
                embedding_budgets.c.repository_id == repository_id,
            )
            .values(
                **new_pos.update_values(),
                write_version=b_row["write_version"] + 1,
                updated_at=now,
            )
        )
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
                reserved_micros=reserved_micros,
                state="reserved",
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                price_snapshot=price_json,
                created_at=now,
                updated_at=now,
            )
        )
        return reservation_id

    async def commit_dispatch_intent(self, org_id: UUID, reservation_id: UUID) -> None:
        if self._is_postgres():
            try:
                await self.s.execute(
                    text("SELECT public.codespy_commit_dispatch_intent(:org_id, :res_id)"),
                    {"org_id": org_id, "res_id": reservation_id},
                )
                return
            except DBAPIError as exc:
                self._map_error(exc)

        stmt = (
            select(model_reservations)
            .where(
                model_reservations.c.org_id == org_id,
                model_reservations.c.reservation_id == reservation_id,
            )
            .with_for_update()
        )
        res = (await self.s.execute(stmt)).mappings().one_or_none()
        if not res:
            from app.services.model_budget import ModelReservationNotFoundError

            raise ModelReservationNotFoundError(f"Reservation {reservation_id} not found")
        if res["state"] == "dispatch_intent_committed":
            return
        if res["state"] != "reserved":
            from app.services.model_budget import ModelInvalidStateTransitionError

            raise ModelInvalidStateTransitionError("Invalid reservation state transition")
        now = await database_now(self.s)
        await self.s.execute(
            update(model_reservations)
            .where(
                model_reservations.c.org_id == org_id,
                model_reservations.c.reservation_id == reservation_id,
            )
            .values(state="dispatch_intent_committed", updated_at=now)
        )

    async def mark_dispatched(self, org_id: UUID, reservation_id: UUID) -> None:
        if self._is_postgres():
            try:
                await self.s.execute(
                    text("SELECT public.codespy_mark_dispatched(:org_id, :res_id)"),
                    {"org_id": org_id, "res_id": reservation_id},
                )
                return
            except DBAPIError as exc:
                self._map_error(exc)

        stmt = (
            select(model_reservations)
            .where(
                model_reservations.c.org_id == org_id,
                model_reservations.c.reservation_id == reservation_id,
            )
            .with_for_update()
        )
        res = (await self.s.execute(stmt)).mappings().one_or_none()
        if not res:
            from app.services.model_budget import ModelReservationNotFoundError

            raise ModelReservationNotFoundError(f"Reservation {reservation_id} not found")
        if res["state"] == "dispatched":
            return
        if res["state"] != "dispatch_intent_committed":
            from app.services.model_budget import ModelInvalidStateTransitionError

            raise ModelInvalidStateTransitionError("Invalid reservation state transition")
        now = await database_now(self.s)
        await self.s.execute(
            update(model_reservations)
            .where(
                model_reservations.c.org_id == org_id,
                model_reservations.c.reservation_id == reservation_id,
            )
            .values(state="dispatched", updated_at=now)
        )

    async def settle_generation(
        self,
        org_id: UUID,
        reservation_id: UUID,
        input_tokens: int,
        output_tokens: int,
        thinking_tokens: int,
        charged_micros: int,
        provider_request_id: str | None,
        actual_cache_read_tokens: int = 0,
        actual_cache_write_tokens: int = 0,
    ) -> None:
        if self._is_postgres():
            try:
                await self.s.execute(
                    text(
                        "SELECT public.codespy_settle_generation("
                        ":org_id, :res_id, :in_tok, :out_tok, :think_tok, "
                        ":charge, :p_req_id, :cache_read, :cache_write)"
                    ),
                    {
                        "org_id": org_id,
                        "res_id": reservation_id,
                        "in_tok": input_tokens,
                        "out_tok": output_tokens,
                        "think_tok": thinking_tokens,
                        "charge": charged_micros,
                        "p_req_id": provider_request_id,
                        "cache_read": actual_cache_read_tokens,
                        "cache_write": actual_cache_write_tokens,
                    },
                )
                return
            except DBAPIError as exc:
                self._map_error(exc)

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
            from app.services.model_budget import ModelReservationNotFoundError

            raise ModelReservationNotFoundError(f"Reservation {reservation_id} not found")
        if res_row["state"] == "settled":
            if res_row["provider_request_id"] != provider_request_id:
                from app.services.model_budget import ModelInvalidStateTransitionError

                raise ModelInvalidStateTransitionError(
                    "Reservation already settled with different provider request ID"
                )
            return
        if res_row["state"] not in ("dispatched", "uncertain_hold"):
            from app.services.model_budget import ModelInvalidStateTransitionError

            raise ModelInvalidStateTransitionError("Cannot settle undispatched reservation")

        stmt_budget = (
            select(embedding_budgets)
            .where(
                embedding_budgets.c.org_id == org_id,
                embedding_budgets.c.repository_id == res_row["repository_id"],
            )
            .with_for_update()
        )
        b_row = (await self.s.execute(stmt_budget)).mappings().one()
        pos = BudgetPosition.from_row(b_row).complete(res_row["reserved_micros"], charged_micros)
        now = await database_now(self.s)
        await self.s.execute(
            update(embedding_budgets)
            .where(
                embedding_budgets.c.org_id == org_id,
                embedding_budgets.c.repository_id == res_row["repository_id"],
            )
            .values(
                **pos.update_values(),
                write_version=b_row["write_version"] + 1,
                updated_at=now,
            )
        )
        await self.s.execute(
            update(model_reservations)
            .where(
                model_reservations.c.org_id == org_id,
                model_reservations.c.reservation_id == reservation_id,
            )
            .values(
                state="settled",
                settled_micros=charged_micros,
                provider_request_id=provider_request_id,
                updated_at=now,
            )
        )
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
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                thinking_tokens=thinking_tokens,
                cache_read_tokens=actual_cache_read_tokens,
                cache_write_tokens=actual_cache_write_tokens,
                charged_micros=charged_micros,
                is_uncertain_hold=False,
                created_at=now,
            )
        )

    async def hold_uncertain(
        self,
        org_id: UUID,
        reservation_id: UUID,
        failure_reason: str = "dispatch_timeout",
    ) -> None:
        if self._is_postgres():
            try:
                await self.s.execute(
                    text("SELECT public.codespy_hold_uncertain(:org_id, :res_id, :reason)"),
                    {"org_id": org_id, "res_id": reservation_id, "reason": failure_reason},
                )
                return
            except DBAPIError as exc:
                self._map_error(exc)

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
            from app.services.model_budget import ModelReservationNotFoundError

            raise ModelReservationNotFoundError(f"Reservation {reservation_id} not found")
        if res_row["state"] == "uncertain_hold":
            return
        if res_row["state"] not in ("dispatched", "dispatch_intent_committed"):
            from app.services.model_budget import ModelInvalidStateTransitionError

            raise ModelInvalidStateTransitionError(
                "Cannot mark uncommitted reservation as uncertain"
            )

        now = await database_now(self.s)
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

    async def release_generation(
        self,
        org_id: UUID,
        reservation_id: UUID,
        failure_reason: str = "cancelled_before_dispatch",
    ) -> None:
        if self._is_postgres():
            try:
                await self.s.execute(
                    text("SELECT public.codespy_release_generation(:org_id, :res_id, :reason)"),
                    {"org_id": org_id, "res_id": reservation_id, "reason": failure_reason},
                )
                return
            except DBAPIError as exc:
                self._map_error(exc)

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
            from app.services.model_budget import ModelReservationNotFoundError

            raise ModelReservationNotFoundError(f"Reservation {reservation_id} not found")
        if res_row["state"] in ("settled", "rejected_before_dispatch"):
            return
        if res_row["state"] != "reserved":
            from app.services.model_budget import ModelInvalidStateTransitionError

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
        position = BudgetPosition.from_row(b_row).complete(reserved_micros)
        now = await database_now(self.s)
        await self.s.execute(
            update(embedding_budgets)
            .where(
                embedding_budgets.c.org_id == org_id,
                embedding_budgets.c.repository_id == repo_id,
            )
            .values(
                **position.update_values(),
                write_version=b_row["write_version"] + 1,
                updated_at=now,
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
                updated_at=now,
            )
        )

    async def _embedding_operation(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        from app.domain.errors import ScopeViolation
        from app.services.provider_budget import (
            BudgetExceededError,
            InvalidReservationTransitionError,
            MissingPriceError,
            ReservationNotFoundError,
        )

        statements = {
            "reserve": "SELECT public.codespy_reserve_embedding_v2(CAST(:request AS jsonb))",
            "settle": (
                "SELECT public.codespy_transition_embedding_v2(:org,:id,CAST(:request AS jsonb))"
            ),
            "cancel": "SELECT public.codespy_cancel_embedding_v3(:org,:id,:reason)",
        }
        try:
            value = await self.s.scalar(text(statements[name]), payload)
        except DBAPIError as error:
            detail = str(error.orig)
            if "embedding_scope_denied" in detail:
                raise ScopeViolation("embedding authority denied") from None
            if "embedding_budget_exceeded" in detail:
                raise BudgetExceededError("embedding budget unavailable") from None
            if "embedding_price_unavailable" in detail:
                raise MissingPriceError("embedding price unavailable") from None
            if "embedding_not_found" in detail:
                raise ReservationNotFoundError("embedding reservation unavailable") from None
            raise InvalidReservationTransitionError("embedding transition rejected") from None
        return dict(value)

    async def reserve_embedding_request(
        self, request: BudgetReservationRequestV1
    ) -> dict[str, Any]:
        return await self._embedding_operation("reserve", {"request": request.model_dump_json()})

    async def transition_embedding_request(
        self, org: UUID, outcome: SettlementOutcomeV1
    ) -> dict[str, Any]:
        return await self._embedding_operation(
            "settle",
            {"org": org, "id": outcome.reservation_id, "request": outcome.model_dump_json()},
        )

    async def cancel_embedding_request(
        self, org: UUID, identity: UUID, reason: str
    ) -> dict[str, Any]:
        return await self._embedding_operation(
            "cancel", {"org": org, "id": identity, "reason": reason}
        )

    async def fund_budget(
        self,
        org_id: UUID,
        audit_id: UUID,
        repository_id: UUID,
        actor_id: UUID,
        request_key: str,
        request_digest: str,
        evidence_digest: str,
        amount_micros: int,
        reason: str,
    ) -> dict[str, Any]:
        if self._is_postgres():
            try:
                stmt = text(
                    "SELECT id, credited_micros, debt_repaid_micros, "
                    "resulting_balance_micros, resulting_reserved_micros, resulting_debt_micros "
                    "FROM public.codespy_fund_budget("
                    ":org_id, :audit_id, :repo_id, :actor_id, :req_key, "
                    ":req_digest, :evi_digest, :amount, :reason)"
                )
                row = (
                    (
                        await self.s.execute(
                            stmt,
                            {
                                "org_id": org_id,
                                "audit_id": audit_id,
                                "repo_id": repository_id,
                                "actor_id": actor_id,
                                "req_key": request_key,
                                "req_digest": request_digest,
                                "evi_digest": evidence_digest,
                                "amount": amount_micros,
                                "reason": reason,
                            },
                        )
                    )
                    .mappings()
                    .one()
                )
                return dict(row)
            except DBAPIError as exc:
                self._map_error(exc)

        # Fallback / SQLite
        stmt_prev = select(budget_funding_receipts).where(
            budget_funding_receipts.c.org_id == org_id,
            budget_funding_receipts.c.request_key == request_key,
        )
        prev = (await self.s.execute(stmt_prev)).mappings().one_or_none()
        if prev:
            if prev["request_digest"] != request_digest:
                raise DuplicateConflict(
                    "duplicate funding request key conflicts with existing audit"
                )
            return {
                "id": prev["id"],
                "credited_micros": prev["credited_micros"],
                "debt_repaid_micros": prev["debt_repaid_micros"],
                "resulting_balance_micros": prev["resulting_balance_micros"],
                "resulting_reserved_micros": prev["resulting_reserved_micros"],
                "resulting_debt_micros": prev["resulting_debt_micros"],
            }

        stmt_b = (
            select(embedding_budgets)
            .where(
                embedding_budgets.c.org_id == org_id,
                embedding_budgets.c.repository_id == repository_id,
            )
            .with_for_update()
        )
        b_row = (await self.s.execute(stmt_b)).mappings().one_or_none()
        if not b_row:
            from app.services.model_budget import ModelBudgetExceededError

            raise ModelBudgetExceededError("Budget account does not exist for repository")

        pos = BudgetPosition.from_row(b_row)
        new_pos, credited, debt_repaid = pos.fund(amount_micros)
        now = await database_now(self.s)
        await self.s.execute(
            update(embedding_budgets)
            .where(
                embedding_budgets.c.org_id == org_id,
                embedding_budgets.c.repository_id == repository_id,
            )
            .values(
                **new_pos.update_values(),
                write_version=b_row["write_version"] + 1,
                updated_at=now,
            )
        )
        await self.s.execute(
            budget_funding_receipts.insert().values(
                org_id=org_id,
                id=audit_id,
                repository_id=repository_id,
                actor_principal_id=actor_id,
                request_key=request_key,
                request_digest=request_digest,
                evidence_digest=evidence_digest,
                amount_micros=amount_micros,
                currency="USD",
                reason=reason,
                credited_micros=credited,
                debt_repaid_micros=debt_repaid,
                resulting_balance_micros=new_pos.balance_micros,
                resulting_reserved_micros=new_pos.reserved_micros,
                resulting_debt_micros=new_pos.debt_micros,
                created_at=now,
            )
        )
        return {
            "id": audit_id,
            "credited_micros": credited,
            "debt_repaid_micros": debt_repaid,
            "resulting_balance_micros": new_pos.balance_micros,
            "resulting_reserved_micros": new_pos.reserved_micros,
            "resulting_debt_micros": new_pos.debt_micros,
        }

    def _map_error(self, exc: DBAPIError) -> None:
        msg = str(exc.orig) if exc.orig else str(exc)
        if "Budget account does not exist" in msg:
            from app.services.model_budget import ModelBudgetExceededError

            raise ModelBudgetExceededError("Budget account does not exist for repository") from exc
        if "Insufficient spendable balance" in msg or "Insufficient balance available" in msg:
            from app.services.model_budget import ModelBudgetExceededError

            raise ModelBudgetExceededError("Insufficient spendable balance or active debt") from exc
        if "Active debt blocks reservation" in msg:
            from app.services.model_budget import ModelBudgetExceededError

            raise ModelBudgetExceededError("Active debt blocks reservation") from exc
        if "Reservation request key conflicts" in msg:
            from app.services.model_budget import ModelInvalidStateTransitionError

            raise ModelInvalidStateTransitionError("Reservation request key conflicts") from exc
        if "Invalid reservation state transition" in msg or "Cannot mark uncommitted" in msg:
            from app.services.model_budget import ModelInvalidStateTransitionError

            raise ModelInvalidStateTransitionError("Invalid reservation state transition") from exc
        if "Reservation not found" in msg:
            from app.services.model_budget import ModelReservationNotFoundError

            raise ModelReservationNotFoundError("Reservation not found") from exc
        if "duplicate funding request key conflicts" in msg:
            raise DuplicateConflict(
                "duplicate funding request key conflicts with existing audit"
            ) from exc
        if getattr(exc.orig, "sqlstate", None) == "23514":
            from app.services.model_budget import ModelInvalidStateTransitionError

            raise ModelInvalidStateTransitionError("Accounting transition rejected") from None
        raise exc
