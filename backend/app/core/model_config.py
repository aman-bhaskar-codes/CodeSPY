"""Explicit non-secret model runtime assembly; no environment or credential discovery."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Literal
from uuid import UUID

import httpx
from pydantic import Field, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.model.bedrock import AsyncConverseClientPort, BedrockModelTransport
from app.adapters.model.gemini import GeminiContentCodec
from app.adapters.model.http_transport import HTTPModelTransport
from app.adapters.model.openai import OpenAIResponsesCodec
from app.contracts.accounting_recovery import AccountingRecoverySettingsV1
from app.contracts.context_manifest import MemoryContextSettingsV1
from app.contracts.model import ModelProvider
from app.contracts.model_configuration import ModelRegistryConfigV1
from app.contracts.provider_budget import DisclosurePolicyV1
from app.domain.scope import Contract
from app.ports.model_transport import ProviderTransportPort
from app.ports.scan_execution import ScanCapabilities
from app.ports.source_reader import SourceReaderPort
from app.services.model_continuations import ContinuationCipher, ModelContinuationService
from app.services.model_registry import ModelRegistry
from app.services.model_runtime import ConfiguredModelRuntime


class RepositoryModelPolicyV1(Contract):
    org_id: UUID
    repository_id: UUID
    policy: DisclosurePolicyV1


class ModelRuntimeSettings(Contract):
    registry: ModelRegistryConfigV1 = Field(default_factory=ModelRegistryConfigV1)
    execution_mode: Literal["offline", "live"] = "offline"
    allow_live_egress: bool = False
    active_prices: tuple[tuple[str, str, str], ...] = ()
    repository_policies: tuple[RepositoryModelPolicyV1, ...] = ()
    memory_context: MemoryContextSettingsV1 = Field(default_factory=MemoryContextSettingsV1)
    accounting_recovery: AccountingRecoverySettingsV1 = Field(
        default_factory=AccountingRecoverySettingsV1
    )


async def unavailable_credentials(reference: str) -> SecretStr:
    raise ValueError("credential provider not configured")


@asynccontextmanager
async def build_model_runtime(
    settings: ModelRuntimeSettings,
    *,
    accounting_factory: async_sessionmaker[AsyncSession],
    admission_factory: async_sessionmaker[AsyncSession] | None = None,
    credentials: Callable[[str], Awaitable[SecretStr]] = unavailable_credentials,
    http_client: httpx.AsyncClient | None = None,
    bedrock_client: AsyncConverseClientPort | None = None,
    continuation_cipher: ContinuationCipher | None = None,
    source_reader: SourceReaderPort | None = None,
    scan_capabilities: ScanCapabilities | None = None,
) -> AsyncIterator[ConfiguredModelRuntime]:
    profiles = [p for p in settings.registry.profiles if p.enabled]
    if settings.execution_mode == "live" and (
        not settings.allow_live_egress
        or any(
            not p.owner_approved_live or not p.locally_qualified or not p.input_bound_qualified
            for p in profiles
        )
    ):
        raise ValueError("live egress requires explicit approval for each enabled profile")
    if any(p.supports_streaming for p in profiles):
        raise ValueError("live streaming exposure is disabled until gateway stream qualification")
    if any(
        p.provider == ModelProvider.BEDROCK_CLAUDE and p.supports_native_schema for p in profiles
    ):
        raise ValueError("native Bedrock schema unsupported by pinned SDK")
    owned = None
    if (
        any(p.provider in (ModelProvider.OPENAI, ModelProvider.GOOGLE_GEMINI) for p in profiles)
        and http_client is None
    ):
        if settings.execution_mode != "live":
            raise ValueError("offline runtime requires an explicitly injected HTTP client")
        owned = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(retries=0),
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
        )
        http_client = owned
    transports: dict[str, ProviderTransportPort] = {}
    if http_client is not None:
        if any(p.provider == ModelProvider.OPENAI for p in profiles):
            transports["openai"] = HTTPModelTransport(
                OpenAIResponsesCodec(), http_client, credentials
            )
        if any(p.provider == ModelProvider.GOOGLE_GEMINI for p in profiles):
            transports["google_gemini"] = HTTPModelTransport(
                GeminiContentCodec(), http_client, credentials
            )
    if bedrock_client is not None and any(
        p.provider == ModelProvider.BEDROCK_CLAUDE for p in profiles
    ):
        transports["bedrock_claude"] = BedrockModelTransport(bedrock_client)
    registered = frozenset(ModelProvider(name) for name in transports)
    policies = {(p.org_id, p.repository_id): p.policy for p in settings.repository_policies}
    if len(policies) != len(settings.repository_policies):
        if owned is not None:
            await owned.aclose()
        raise ValueError("duplicate repository disclosure policy")
    try:
        yield ConfiguredModelRuntime(
            registry=ModelRegistry(
                settings.registry,
                registered_providers=registered,
                active_prices=frozenset(settings.active_prices),
                execution_mode=settings.execution_mode,
            ),
            transports=transports,
            accounting_factory=accounting_factory,
            continuations=ModelContinuationService(continuation_cipher)
            if continuation_cipher
            else None,
            admission_factory=admission_factory,
            disclosure_policies=policies,
            memory_context=settings.memory_context,
            accounting_recovery=settings.accounting_recovery,
            source_reader=source_reader,
            scan_capabilities=scan_capabilities,
        )
    finally:
        if owned is not None:
            await owned.aclose()
