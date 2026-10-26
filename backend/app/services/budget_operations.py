"""Owner-authorized positive budget funding and account initialization with immutable receipts."""

from uuid import UUID, uuid4

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.budget_operations import BudgetFundingReceiptV1, FundBudgetV1
from app.db.transaction import transaction
from app.domain.canonical_json import semantic_digest
from app.domain.errors import DuplicateConflict, ScopeViolation
from app.domain.scope import OrgRole, TenantContext
from app.models.budget_tables import budget_funding_receipts, embedding_budgets
from app.security.authorization import require_repository
from app.services.model_budget import ModelBudgetExceededError


class BudgetOperationsService:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory

    async def init_budget(
        self,
        actor: TenantContext,
        repository_id: UUID,
        currency: str = "USD",
    ) -> None:
        """Explicitly initialize a zero-funded budget account if not already present."""
        async with transaction(self.factory) as session:
            await require_repository(
                session, actor.org_id, actor.principal_id, repository_id, write=True
            )
            stmt = select(embedding_budgets).where(
                embedding_budgets.c.org_id == actor.org_id,
                embedding_budgets.c.repository_id == repository_id,
            )
            existing = (await session.execute(stmt)).mappings().one_or_none()
            if existing is None:
                await session.execute(
                    insert(embedding_budgets).values(
                        org_id=actor.org_id,
                        repository_id=repository_id,
                        currency=currency,
                        balance_micros=0,
                        reserved_micros=0,
                        debt_micros=0,
                        write_version=1,
                    )
                )

    async def fund(
        self,
        actor: TenantContext,
        repository_id: UUID,
        request: FundBudgetV1,
    ) -> BudgetFundingReceiptV1:
        """Atomically fund an account with exact debt repayment and immutable audit receipt."""
        request = FundBudgetV1.model_validate(request.model_dump())
        async with transaction(self.factory) as session:
            current = await require_repository(
                session, actor.org_id, actor.principal_id, repository_id, write=True
            )
            if current.role != OrgRole.OWNER:
                raise ScopeViolation("owner authorization required")

            binding = {
                "org_id": str(actor.org_id),
                "repository_id": str(repository_id),
                "actor_id": str(actor.principal_id),
                **request.model_dump(mode="json"),
            }
            digest = semantic_digest(binding)

            # Idempotent receipt lookup
            previous = (
                (
                    await session.execute(
                        select(budget_funding_receipts).where(
                            budget_funding_receipts.c.org_id == actor.org_id,
                            budget_funding_receipts.c.request_key == request.request_key,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if previous is not None:
                if previous["request_digest"] != digest:
                    raise DuplicateConflict("funding request conflicts")
                return BudgetFundingReceiptV1(
                    audit_id=previous["id"],
                    credited_micros=previous["credited_micros"],
                    debt_repaid_micros=previous["debt_repaid_micros"],
                    resulting_balance_micros=previous["resulting_balance_micros"],
                    resulting_reserved_micros=previous["resulting_reserved_micros"],
                    resulting_debt_micros=previous["resulting_debt_micros"],
                )

            # Lock budget row
            stmt_budget = (
                select(embedding_budgets)
                .where(
                    embedding_budgets.c.org_id == actor.org_id,
                    embedding_budgets.c.repository_id == repository_id,
                )
                .with_for_update()
            )
            b_row = (await session.execute(stmt_budget)).mappings().one_or_none()
            if b_row is None:
                raise ModelBudgetExceededError("Budget account does not exist for repository")

            current = await require_repository(
                session, actor.org_id, actor.principal_id, repository_id, write=True
            )
            if current.role != OrgRole.OWNER:
                raise ScopeViolation("owner authorization required")

            from app.repositories.accounting_repository import AccountingRepository

            repo = AccountingRepository(session)
            receipt_dict = await repo.fund_budget(
                org_id=actor.org_id,
                audit_id=uuid4(),
                repository_id=repository_id,
                actor_id=actor.principal_id,
                request_key=request.request_key,
                request_digest=digest,
                evidence_digest=request.evidence_digest,
                amount_micros=request.amount_micros,
                reason=request.reason,
            )

            return BudgetFundingReceiptV1(
                audit_id=receipt_dict["id"],
                credited_micros=receipt_dict["credited_micros"],
                debt_repaid_micros=receipt_dict["debt_repaid_micros"],
                resulting_balance_micros=receipt_dict["resulting_balance_micros"],
                resulting_reserved_micros=receipt_dict["resulting_reserved_micros"],
                resulting_debt_micros=receipt_dict["resulting_debt_micros"],
            )
