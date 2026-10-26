"""Injected provider runtime; configuration never discovers credentials or model access."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.accounting_recovery import AccountingRecoverySettingsV1
from app.contracts.context_manifest import MemoryContextSettingsV1
from app.contracts.model import ModelRoutingRequirementV1
from app.contracts.model_availability import (
    ModelAvailabilityItemV1,
    ModelAvailabilityStatus,
    ModelAvailabilityV1,
)
from app.contracts.provider_budget import DisclosurePolicyV1
from app.db.transaction import transaction
from app.ports.investigation_execution import ModelDispatchAuthorityPort, ModelResultStorePort
from app.ports.model_transport import ProviderTransportPort
from app.ports.scan_execution import ScanCapabilities
from app.ports.source_reader import SourceReaderPort
from app.services.configured_model_gateway import ConfiguredModelGateway
from app.services.memory_evidence import SourceMemoryEvidence
from app.services.model_continuations import ModelContinuationService
from app.services.model_registry import ModelRegistry, ModelRegistryError
from app.workers.accounting_recovery import AccountingRecoveryWorker
from app.workers.memory_maintenance import MemoryMaintenanceWorker


@dataclass(frozen=True)
class ConfiguredModelRuntime:
    registry: ModelRegistry
    transports: Mapping[str, ProviderTransportPort]
    accounting_factory: async_sessionmaker[AsyncSession]
    continuations: ModelContinuationService | None = None
    admission_factory: async_sessionmaker[AsyncSession] | None = None

    disclosure_policies: Mapping[tuple[UUID, UUID], DisclosurePolicyV1] = field(
        default_factory=dict
    )
    memory_context: MemoryContextSettingsV1 = field(default_factory=MemoryContextSettingsV1)
    source_reader: SourceReaderPort | None = None
    scan_capabilities: ScanCapabilities | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "transports", MappingProxyType(dict(self.transports)))
        object.__setattr__(
            self, "disclosure_policies", MappingProxyType(dict(self.disclosure_policies))
        )

    accounting_recovery: AccountingRecoverySettingsV1 = field(
        default_factory=AccountingRecoverySettingsV1
    )

    async def maintenance_tick(self) -> int:
        """One bounded recovery/retention pass for the existing worker scheduler."""
        summary = await AccountingRecoveryWorker(
            self.accounting_factory,
            self.accounting_recovery,
        ).tick()
        async with transaction(self.accounting_factory) as session:
            removed = await session.scalar(
                text("SELECT public.codespy_expire_model_continuations(100)")
            )
        async with transaction(self.accounting_factory) as session:
            if await session.scalar(
                text("SELECT to_regproc('public.codespy_expire_model_results')")
            ):
                removed = int(removed or 0) + int(
                    await session.scalar(text("SELECT public.codespy_expire_model_results(100)"))
                    or 0
                )
        purged = await MemoryMaintenanceWorker(self.accounting_factory).tick()
        return summary.held + int(removed or 0) + purged

    def policy_for(self, org_id: UUID, repository_id: UUID) -> DisclosurePolicyV1:
        return self.disclosure_policies.get((org_id, repository_id), DisclosurePolicyV1())

    def gateway(
        self,
        *,
        source_reader: SourceReaderPort | None = None,
        scan_capabilities: ScanCapabilities | None = None,
        result_store: ModelResultStorePort | None = None,
        dispatch_authority: ModelDispatchAuthorityPort | None = None,
    ) -> ConfiguredModelGateway:
        return ConfiguredModelGateway(
            self.accounting_factory,
            self.registry,
            self.transports,
            continuations=self.continuations,
            admission_factory=self.admission_factory,
            result_store=result_store,
            dispatch_authority=dispatch_authority,
            memory_context=self.memory_context,
            memory_evidence_factory=lambda session: SourceMemoryEvidence(
                session,
                self.accounting_factory,
                source_reader or self.source_reader,
                scan_capabilities or self.scan_capabilities,
            ),
        )

    def availability(self, org_id: UUID, repository_id: UUID) -> ModelAvailabilityV1:
        policy = self.policy_for(org_id, repository_id)
        items = []
        for profile in self.registry.config.profiles:
            eligible = False
            try:
                self.registry.resolve(
                    ModelRoutingRequirementV1(
                        task_type=profile.allowed_task_types[0],
                        estimated_context_tokens=1,
                        estimated_output_tokens=1,
                    ),
                    policy,
                    profile.alias,
                )
                eligible = profile.provider.value in self.transports
            except ModelRegistryError:
                pass
            status: ModelAvailabilityStatus = (
                "disabled"
                if not profile.enabled
                else "unavailable"
                if not eligible
                else "live_unqualified"
                if self.registry.execution_mode == "live"
                else "locally_qualified"
            )
            items.append(
                ModelAvailabilityItemV1(
                    alias=profile.alias,
                    provider=profile.provider.value,
                    enabled=profile.enabled,
                    locally_qualified=profile.locally_qualified,
                    owner_approved_live=profile.owner_approved_live,
                    eligible=eligible,
                    status=status,
                    supports_tools=profile.supports_tools,
                    supports_native_schema=profile.supports_native_schema,
                )
            )
        return ModelAvailabilityV1(profiles=tuple(items))
