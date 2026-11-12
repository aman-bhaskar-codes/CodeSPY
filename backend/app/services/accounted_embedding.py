"""Budget-accounted, disclosure-checked embedding calls.

``AccountedEmbeddingGate`` wraps any ``EmbeddingPort``. For one scope (organisation, repository,
principal, disclosure policy and operation) it:

1. refuses providers/regions the repository policy does not permit and requests that the adapter
   could never send (nothing is reserved);
2. reserves budget and commits it;
3. commits the dispatch claim *before* the external call, so only one caller can ever send it;
4. calls the provider once, then settles the actual token charge.

After the claim a failure or timeout settles as an uncertain hold, which only an operator or the
accounting recovery worker resolves; a hold is never released on elapsed time. Reservation and
dispatch use separate session factories because the database roles differ.
"""

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.adapters.model.http_transport import ProviderTransportError
from app.contracts.provider_budget import (
    BudgetReservationRequestV1,
    DisclosurePolicyV1,
    EmbeddingRequestV1,
    EmbeddingResponseV1,
    ReservationState,
    SettlementOutcomeV1,
    calculate_charge_micros,
)
from app.ports.embedding import EmbeddingPort
from app.services.provider_budget import (
    DisclosureDeniedError,
    ProviderBudgetError,
    ProviderBudgetService,
)

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], Any]  # async_sessionmaker[AsyncSession]


class EmbeddingReplayError(ProviderBudgetError):
    """The logical call was already reserved or settled; the vector cannot be reproduced."""


class EmbeddingDispatchError(ProviderBudgetError):
    """Another caller holds the dispatch claim; this call must not be sent."""


@dataclass(frozen=True)
class EmbeddingScope:
    org_id: UUID
    repository_id: UUID
    principal_id: UUID
    policy: DisclosurePolicyV1
    operation_key: str  # one per logical operation (job attempt, query); same key + text replays
    region: str = "ap-south-1"


def estimated_tokens(text: str) -> int:
    """Conservative upper bound: one token per three UTF-8 bytes, at least one."""
    return max(1, -(-len(text.encode()) // 3))


def idempotency_key(scope: EmbeddingScope, request: EmbeddingRequestV1) -> str:
    material = "\x1f".join(
        (
            scope.operation_key,
            str(scope.org_id),
            str(scope.repository_id),
            request.provider,
            request.model_id,
            str(request.dimensions),
            str(request.normalize),
            request.text,
        )
    )
    return "emb:" + hashlib.sha256(material.encode()).hexdigest()[:48]


class AccountedEmbeddingGate:
    def __init__(
        self,
        inner: EmbeddingPort,
        reserve_sessions: SessionFactory,
        dispatch_sessions: SessionFactory,
    ) -> None:
        self.inner = inner
        self.reserve_sessions = reserve_sessions
        self.dispatch_sessions = dispatch_sessions

    def bind(self, scope: EmbeddingScope) -> EmbeddingPort:
        return _BoundPort(self, scope)

    async def embed(
        self, scope: EmbeddingScope, request: EmbeddingRequestV1
    ) -> EmbeddingResponseV1:
        if not scope.policy.is_provider_permitted(request.provider):
            raise DisclosureDeniedError("provider is not permitted by the repository policy")
        if not scope.policy.is_region_permitted(scope.region):
            raise DisclosureDeniedError("region is not permitted by the repository policy")
        validate = getattr(self.inner, "validate", None)
        if validate is not None:
            validate(request)  # refuse unsendable requests before any claim exists

        async with self.reserve_sessions() as session, session.begin():
            price = await ProviderBudgetService(session).get_active_price(
                request.provider, request.model_id, datetime.now(UTC)
            )
        tokens = estimated_tokens(request.text)
        ceiling = max(1, calculate_charge_micros(tokens, price.input_micros_per_1k))
        reservation_request = BudgetReservationRequestV1(
            org_id=scope.org_id,
            repository_id=scope.repository_id,
            principal_id=scope.principal_id,
            provider=request.provider,
            model_id=request.model_id,
            estimated_tokens=tokens,
            max_charge_micros=ceiling,
            idempotency_key=idempotency_key(scope, request),
        )
        async with self.reserve_sessions() as session, session.begin():
            reservation = await ProviderBudgetService(session).reserve(
                reservation_request, scope.policy
            )
        if reservation.state is not ReservationState.RESERVED:
            raise EmbeddingReplayError("embedding call already settled or on hold")

        async with self.dispatch_sessions() as session, session.begin():
            claimed = await ProviderBudgetService(session).mark_dispatched(
                scope.org_id, reservation.reservation_id
            )
        if not claimed:
            raise EmbeddingDispatchError("dispatch already claimed")

        try:
            response = await self.inner.embed(request)
        except BaseException as error:
            failure = "timeout" if _is_timeout(error) else "failed"
            await self._settle(scope, reservation.reservation_id, failure, 0, 0)
            raise
        charge = calculate_charge_micros(response.token_count, price.input_micros_per_1k)
        await self._settle(
            scope, reservation.reservation_id, "success", response.token_count, charge
        )
        return response

    async def _settle(
        self, scope: EmbeddingScope, reservation_id: UUID, status: Any, tokens: int, charge: int
    ) -> None:
        outcome = SettlementOutcomeV1(
            reservation_id=reservation_id,
            status=status,
            actual_tokens=tokens,
            actual_charge_micros=charge,
        )
        try:
            async with self.dispatch_sessions() as session, session.begin():
                await ProviderBudgetService(session).settle(scope.org_id, outcome)
        except Exception:
            # The claim stays; recovery turns a stale claim into an uncertain hold. Never log
            # payloads or the exception text, which may carry database detail.
            logger.error("embedding settlement failed; recovery will hold the reservation")
            if status != "success":
                raise


def _is_timeout(error: BaseException) -> bool:
    return isinstance(error, TimeoutError) or (
        isinstance(error, ProviderTransportError) and error.code == "provider_timeout"
    )


class _BoundPort:
    def __init__(self, gate: AccountedEmbeddingGate, scope: EmbeddingScope) -> None:
        self.gate, self.scope = gate, scope

    async def embed(self, request: EmbeddingRequestV1) -> EmbeddingResponseV1:
        return await self.gate.embed(self.scope, request)


__all__ = [
    "AccountedEmbeddingGate",
    "EmbeddingDispatchError",
    "EmbeddingReplayError",
    "EmbeddingScope",
]
