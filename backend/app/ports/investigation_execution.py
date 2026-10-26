"""Typed ports for durable investigation execution.

Defines the abstract interfaces that the investigation executor, tools,
model result store, job authority and dispatch authority depend on.
Implementations are injected; these ports carry no business logic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from uuid import UUID

from app.contracts.investigation_execution import (
    InvestigationAdvanceV1,
    InvestigationAuthorityV1,
    InvestigationExecutionCheckpointV1,
    InvestigationExecutionContextV1,
    InvestigationExecutionStateV1,
    InvestigationScopeV1,
    InvestigationToolRequestV1,
    InvestigationToolResultV1,
    ModelInvocationRefV1,
)
from app.contracts.jobs import AttemptLeaseV2
from app.contracts.model import ModelResponseV1, ToolDefinitionV1
from app.domain.scope import TenantContext
from sqlalchemy.ext.asyncio import AsyncSession


class InvestigationToolPort(ABC):
    """Route validated read-only tool requests through scoped adapters."""

    def get_tool_definitions(self) -> tuple[ToolDefinitionV1, ...]:
        """Advertise only explicitly registered capabilities."""
        return ()

    @abstractmethod
    async def execute(
        self,
        request: InvestigationToolRequestV1,
        context: InvestigationExecutionContextV1,
    ) -> InvestigationToolResultV1:
        """Execute a scoped tool invocation.

        Never reads from host disk directly. Returns bounded safe
        payload or explicit unavailable/denied status.
        """


class InvestigationExecutorPort(ABC):
    """Advance durable investigation node state."""

    @abstractmethod
    async def advance(
        self,
        context: InvestigationExecutionContextV1,
    ) -> InvestigationAdvanceV1:
        """Advance the investigation by one node.

        Returns progress, ready_to_publish, needs_review or terminal_failure
        with durable cursor references.
        """


class InvestigationJobAuthorityPort(ABC):
    """Assert current job/attempt authority for investigation writes."""

    @abstractmethod
    async def assert_current(
        self,
        session: AsyncSession,
        lease: AttemptLeaseV2,
        *,
        lock: bool = False,
    ) -> InvestigationAuthorityV1:
        """Check current authority against DB time and job state.

        If lock=True, acquires the investigation row lock in the
        established order (job -> attempt -> investigation).
        Raises if expired, cancelled, revoked or superseded.
        """


class ModelDispatchAuthorityPort(ABC):
    """Pre-dispatch authority check bound to investigation job context."""

    @abstractmethod
    async def assert_dispatch_allowed(
        self,
        session: AsyncSession,
        investigation_id: UUID,
        logical_call_id: UUID,
    ) -> None:
        """Verify current job/spec/lease/cancellation before dispatch marker.

        Must share the same transaction as the dispatch marker claim.
        Raises on expired/cancelled/revoked authority.
        """


class ModelResultStorePort(ABC):
    """Scoped durable store for encrypted model call results."""

    @abstractmethod
    async def get(
        self,
        invocation: ModelInvocationRefV1,
        actor: TenantContext | None = None,
    ) -> ModelResponseV1 | None:
        """Retrieve a retained result for the exact invocation identity.

        Returns None if no result exists, the result is expired,
        or scope/profile/actor does not match.
        """

    @abstractmethod
    async def put_known(
        self,
        session: AsyncSession,
        invocation: ModelInvocationRefV1,
        response: ModelResponseV1,
        *,
        failure_code: str | None = None,
    ) -> UUID:
        """Store an encrypted result atomically with settlement.

        Returns the result record ID. Exact replay returns the
        existing ID without mutation.
        """


class InvestigationExecutionCheckpointRepository(ABC):
    """Repository for versioned execution checkpoints."""

    @abstractmethod
    async def load_latest(
        self,
        session: AsyncSession,
        scope: InvestigationScopeV1,
    ) -> InvestigationExecutionCheckpointV1 | None:
        """Load the most recent checkpoint for the investigation execution."""

    @abstractmethod
    async def append(
        self,
        session: AsyncSession,
        lease: AttemptLeaseV2,
        *,
        expected_previous_sequence: int,
        state: InvestigationExecutionStateV1,
    ) -> InvestigationExecutionCheckpointV1:
        """Append a new checkpoint under current authority.

        Checks exact replay/digest, current authority and expected
        predecessor under the investigation lock.
        """


class InvestigationSupportPort(ABC):
    """Validate retained snapshot and current finding support without source I/O."""

    @abstractmethod
    async def assert_current(self, session: AsyncSession, scope: InvestigationScopeV1) -> None: ...
