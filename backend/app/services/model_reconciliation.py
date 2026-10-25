"""Owner-authorized atomic settlement from independently verified usage receipts."""

from uuid import UUID, uuid4

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.model_reconciliation import ModelReconciliationReceiptV1, ReconcileModelUsageV1
from app.db.transaction import transaction
from app.domain.canonical_json import semantic_digest
from app.domain.errors import DuplicateConflict, InvalidTransition, ScopeViolation
from app.domain.scope import OrgRole, TenantContext
from app.models.model_gateway_tables import model_reservations, model_usage_reconciliations
from app.security.authorization import require_repository
from app.services.model_budget import ModelBudgetService


class ModelReconciliationService:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory

    async def reconcile(
        self, actor: TenantContext, repository_id: UUID, request: ReconcileModelUsageV1
    ) -> ModelReconciliationReceiptV1:
        request = ReconcileModelUsageV1.model_validate(request.model_dump())
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
            reservation = (
                (
                    await session.execute(
                        select(model_reservations)
                        .where(
                            model_reservations.c.org_id == actor.org_id,
                            model_reservations.c.repository_id == repository_id,
                            model_reservations.c.reservation_id == request.reservation_id,
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if reservation is None:
                raise ScopeViolation("reservation unavailable")
            current = await require_repository(
                session, actor.org_id, actor.principal_id, repository_id, write=True
            )
            if current.role != OrgRole.OWNER:
                raise ScopeViolation("owner authorization required")
            previous = (
                (
                    await session.execute(
                        select(model_usage_reconciliations).where(
                            model_usage_reconciliations.c.org_id == actor.org_id,
                            model_usage_reconciliations.c.request_key == request.request_key,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if previous is not None:
                if previous["request_digest"] != digest:
                    raise DuplicateConflict("reconciliation request conflicts")
                return ModelReconciliationReceiptV1(
                    audit_id=previous["id"],
                    reservation_id=request.reservation_id,
                    charged_micros=previous["charged_micros"],
                    evidence_digest=previous["evidence_digest"],
                )
            if reservation["state"] != "uncertain_hold":
                raise InvalidTransition("only uncertain usage can be reconciled")
            budget = ModelBudgetService(session)
            price = await budget.get_reserved_price(actor.org_id, request.reservation_id)
            usage = request.usage
            charge = price.calculate_cost_micros(**usage.model_dump())
            await budget.settle_reservation(
                actor.org_id,
                request.reservation_id,
                usage.input_tokens,
                usage.output_tokens,
                usage.thinking_tokens,
                charge,
                request.provider_request_id,
                actual_cache_read_tokens=usage.cache_read_tokens,
                actual_cache_write_tokens=usage.cache_write_tokens,
            )
            audit_id = uuid4()
            await session.execute(
                insert(model_usage_reconciliations).values(
                    org_id=actor.org_id,
                    id=audit_id,
                    repository_id=repository_id,
                    reservation_id=request.reservation_id,
                    actor_principal_id=actor.principal_id,
                    request_key=request.request_key,
                    request_digest=digest,
                    evidence_digest=request.evidence_digest,
                    provider_request_id=request.provider_request_id,
                    price_digest=semantic_digest(price.model_dump(mode="json")),
                    usage_json=usage.model_dump(),
                    reason=request.reason,
                    charged_micros=charge,
                )
            )
            return ModelReconciliationReceiptV1(
                audit_id=audit_id,
                reservation_id=request.reservation_id,
                charged_micros=charge,
                evidence_digest=request.evidence_digest,
            )
