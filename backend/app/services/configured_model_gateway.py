"""Durable single-dispatch execution for configured provider transports."""

import asyncio
import json
import logging
from collections.abc import Callable, Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.model.codec_support import validate_output_schema
from app.contracts.context_manifest import ContextManifestV1, MemoryContextSettingsV1
from app.contracts.investigation_execution import ModelInvocationRefV1
from app.contracts.model import (
    ChatMessageV1,
    ModelRequestV1,
    ModelResponseV1,
    ModelRoutingRequirementV1,
    ToolDefinitionV1,
)
from app.contracts.model_transport import ProviderUsageEvidenceV1
from app.contracts.provider_budget import DisclosurePolicyV1
from app.db.transaction import transaction
from app.domain.canonical_json import semantic_digest
from app.domain.errors import ScopeViolation
from app.models.model_gateway_tables import model_reservations
from app.ports.investigation_execution import (
    ModelDispatchAuthorityPort,
    ModelResultStorePort,
)
from app.ports.memory import MemoryEvidencePort
from app.ports.model_transport import ProviderTransportPort
from app.security.authorization import require_repository
from app.services.investigation_context import InvestigationContextService
from app.services.memory_service import MemoryService
from app.services.model_accounting_authority import (
    AuthorityModelBudgetService as ModelBudgetService,
)
from app.services.model_budget import ModelInvalidStateTransitionError
from app.services.model_continuations import ModelContinuationService
from app.services.model_registry import ModelRegistry, ModelRegistryError
from app.services.model_router import ModelRouter
from app.services.model_usage_receipts import record_usage_receipt
from app.services.output_validator import OutputValidator

logger = logging.getLogger(__name__)


class ModelConnectionError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ConfiguredModelGateway:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        registry: ModelRegistry,
        transports: Mapping[str, ProviderTransportPort],
        *,
        continuations: ModelContinuationService | None = None,
        admission_factory: async_sessionmaker[AsyncSession] | None = None,
        memory_context: MemoryContextSettingsV1 | None = None,
        memory_evidence_factory: Callable[[AsyncSession], MemoryEvidencePort] | None = None,
        result_store: ModelResultStorePort | None = None,
        dispatch_authority: ModelDispatchAuthorityPort | None = None,
    ) -> None:
        self.factory = factory
        self.admission_factory = admission_factory or factory
        self.registry = registry
        self.transports = dict(transports)
        self.continuations = continuations
        self.memory_context = memory_context or MemoryContextSettingsV1()
        self.memory_evidence_factory = memory_evidence_factory
        self.result_store = result_store
        self.dispatch_authority = dispatch_authority

    def _memories(self, session: AsyncSession) -> MemoryService:
        return MemoryService(
            session,
            evidence=self.memory_evidence_factory(session)
            if self.memory_evidence_factory
            else None,
        )

    async def execute_call(
        self,
        org_id: UUID,
        repository_id: UUID,
        principal_id: UUID,
        investigation_id: UUID,
        logical_call_id: UUID,
        messages: tuple[ChatMessageV1, ...],
        requirement: ModelRoutingRequirementV1,
        disclosure_policy: DisclosurePolicyV1,
        idempotency_key: str,
        tools: tuple[ToolDefinitionV1, ...] | None = None,
        preferred_provider: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.0,
        response_schema: dict[str, Any] | None = None,
        attempt_id: int = 1,
        requested_alias: str | None = None,
    ) -> ModelResponseV1:
        limit = requirement.estimated_output_tokens if max_tokens is None else max_tokens
        if type(limit) is not int or not 1 <= limit <= 32768:
            raise ValueError("invalid model output limit")
        bounded = requirement.model_copy(update={"estimated_output_tokens": limit})
        resolved = self.registry.resolve(bounded, disclosure_policy, requested_alias)
        profile = resolved.profile
        if preferred_provider is not None and preferred_provider != profile.provider.value:
            raise ModelRegistryError()
        transport = self.transports.get(profile.provider.value)
        if transport is None:
            raise ModelRegistryError()
        if (profile.reasoning_mode != "none" or tools) and self.continuations is None:
            raise ModelConnectionError("continuation_storage_unavailable")
        context = {}
        async with transaction(self.factory) as session:
            actor = await require_repository(
                session, org_id, principal_id, repository_id, write=True
            )
            for message in messages:
                if message.continuation_ref is None:
                    continue
                if self.continuations is None:
                    raise ModelConnectionError("continuation_storage_unavailable")
                value = await self.continuations.get(
                    session,
                    actor,
                    repository_id,
                    investigation_id,
                    message.continuation_ref,
                    resolved.profile_digest,
                )
                context[str(message.continuation_ref)] = json.loads(value)
        request = ModelRequestV1(
            investigation_id=investigation_id,
            call_id=logical_call_id,
            attempt_id=attempt_id,
            provider=profile.provider.value,
            model_id=profile.model_id,
            messages=messages,
            tools=tools or (),
            max_tokens=limit,
            temperature=temperature,
            response_schema=response_schema,
            reservation_id=UUID(int=0),
            opaque_context_json=json.dumps(context, separators=(",", ":")),
        )
        manifest: ContextManifestV1 | None = None
        if self.memory_context.mode != "disabled":
            async with transaction(self.factory) as session:
                composed, manifest = await InvestigationContextService(
                    session,
                    self._memories(session),
                    self.memory_context,
                ).prepare(
                    org_id,
                    repository_id,
                    principal_id,
                    request,
                    profile,
                    transport,
                    semantic_digest(disclosure_policy.model_dump(mode="json")),
                )
                wire = composed.prepared
                request = wire.request
        else:
            wire = transport.prepare(request, profile)
        upper = max(
            requirement.estimated_context_tokens,
            len(wire.body_json.encode()) + 4096 + 64 * len(messages),
        )
        if upper + limit + profile.thinking_token_budget > profile.max_context_tokens:
            raise ModelConnectionError("context_bound_exceeded")
        bounded = bounded.model_copy(update={"estimated_context_tokens": upper})
        digest = semantic_digest(
            {
                "request": request.model_dump(mode="json"),
                "wire_digest": wire.wire_digest,
                "org": org_id,
                "repo": repository_id,
                "principal": principal_id,
                "policy": disclosure_policy.model_dump(mode="json"),
                "bound": bounded.model_dump(mode="json"),
            }
        )
        async with transaction(self.admission_factory) as session:
            await require_repository(session, org_id, principal_id, repository_id, write=True)
            routed = await ModelRouter(
                ModelBudgetService(session), registry=self.registry
            ).route_and_reserve(
                org_id,
                repository_id,
                principal_id,
                investigation_id,
                logical_call_id,
                attempt_id,
                bounded,
                disclosure_policy,
                idempotency_key,
                preferred_provider,
                digest,
                profile.alias,
            )
        identity = routed.reservation_id
        invocation_ref: ModelInvocationRefV1 | None = None
        if self.result_store is not None:
            invocation_ref = ModelInvocationRefV1(
                org_id=org_id,
                repository_id=repository_id,
                principal_id=principal_id,
                investigation_id=investigation_id,
                node_id=logical_call_id,
                reservation_id=identity,
                request_digest=digest,
                profile_digest=resolved.profile_digest,
            )

        async with transaction(self.factory) as session:
            current_state = await session.scalar(
                select(model_reservations.c.state).where(
                    model_reservations.c.org_id == org_id,
                    model_reservations.c.reservation_id == identity,
                )
            )
            if current_state == "settled":
                if self.result_store is not None and invocation_ref is not None:
                    actor = await require_repository(
                        session, org_id, principal_id, repository_id, write=True
                    )
                    retained = await self.result_store.get(invocation_ref, actor)
                    if retained is not None:
                        return retained
                    raise ModelConnectionError("model_result_unavailable")
                raise ModelConnectionError("model_call_already_completed")
            price = await ModelBudgetService(session).get_reserved_price(org_id, identity)
        claimed = False

        async def commit_dispatch() -> None:
            nonlocal claimed
            async with transaction(self.factory) as session:
                await require_repository(session, org_id, principal_id, repository_id, write=True)
                if manifest is not None:
                    await InvestigationContextService(
                        session,
                        self._memories(session),
                        self.memory_context,
                    ).assert_current(manifest)
                if self.dispatch_authority is not None:
                    await self.dispatch_authority.assert_dispatch_allowed(
                        session, investigation_id, logical_call_id
                    )
                budget = ModelBudgetService(session)
                if session.bind is not None and session.bind.dialect.name == "postgresql":
                    state = await session.scalar(
                        text("SELECT public.codespy_claim_model_dispatch(:org,:res)"),
                        {"org": org_id, "res": identity},
                    )
                else:
                    state = await session.scalar(
                        select(model_reservations.c.state).where(
                            model_reservations.c.org_id == org_id,
                            model_reservations.c.reservation_id == identity,
                        )
                    )
                    if state == "reserved":
                        await budget.commit_dispatch_intent(org_id, identity)
                        await budget.mark_dispatched(org_id, identity)
                        state = "claimed"
                if state != "claimed":
                    raise ModelConnectionError(
                        "model_call_already_completed"
                        if state == "settled"
                        else "model_call_pending_or_terminal"
                    )
            claimed = True

        wire = wire.model_copy(
            update={"request": request.model_copy(update={"reservation_id": identity})}
        )
        try:
            result = await transport.invoke(wire, before_dispatch=commit_dispatch)
            if manifest is not None and result.response is not None:
                result = result.model_copy(
                    update={
                        "response": result.response.model_copy(
                            update={
                                "context_packet_hash": manifest.selection_digest,
                                "memory_provenance_hash": semantic_digest(
                                    {
                                        "scope": manifest.scope.model_dump(),
                                        "references": [
                                            r.model_dump() for r in manifest.memory_references
                                        ],
                                    }
                                ),
                            }
                        )
                    }
                )
            evidence = result.usage
            if evidence.usage is None:
                await self._hold(org_id, repository_id, identity, resolved.profile_digest, evidence)
                raise ModelConnectionError("model_usage_unknown")
            usage = evidence.usage
            if any(
                (count > 0 and rate <= 0)
                for count, rate in (
                    (usage.thinking_tokens, price.thinking_micros_per_1k),
                    (usage.cache_read_tokens, price.cache_read_micros_per_1k),
                    (usage.cache_write_tokens, price.cache_write_micros_per_1k),
                )
            ):
                await self._hold(
                    org_id,
                    repository_id,
                    identity,
                    resolved.profile_digest,
                    ProviderUsageEvidenceV1(
                        quality="unknown",
                        reason="unsupported_usage",
                        raw_counters_json=evidence.raw_counters_json,
                    ),
                )
                raise ModelConnectionError("model_usage_unpriced")
            charge = price.calculate_cost_micros(**usage.model_dump())
            continuation = None
            denied = False
            unsupported_continuation = False
            async with transaction(self.factory) as session:
                await ModelBudgetService(session).settle_reservation(
                    org_id,
                    identity,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.thinking_tokens,
                    charge,
                    result.response.provider_request_id if result.response else None,
                    actual_cache_read_tokens=usage.cache_read_tokens,
                    actual_cache_write_tokens=usage.cache_write_tokens,
                )
                await record_usage_receipt(
                    session, org_id, repository_id, identity, resolved.profile_digest, evidence
                )
                if result.continuation_json is not None:
                    if self.continuations is None or (
                        result.response and result.response.tool_calls and not tools
                    ):
                        unsupported_continuation = True
                    else:
                        try:
                            continuation = await self.continuations.put(
                                session,
                                actor,
                                repository_id,
                                investigation_id,
                                identity,
                                resolved.profile_digest,
                                result.continuation_json,
                            )
                        except ScopeViolation:
                            denied = True
                if (
                    self.result_store is not None
                    and invocation_ref is not None
                    and result.response is not None
                ):
                    candidate_resp = result.response.model_copy(
                        update={"charge_micros": charge, "continuation_ref": continuation}
                    )
                    await self.result_store.put_known(
                        session,
                        invocation_ref,
                        candidate_resp,
                        failure_code=result.failure_code,
                    )
        except (Exception, asyncio.CancelledError):
            if claimed:
                await self._hold(org_id, repository_id, identity, resolved.profile_digest)
            else:
                try:
                    async with transaction(self.admission_factory) as session:
                        await ModelBudgetService(session).release_reservation(org_id, identity)
                except (ModelInvalidStateTransitionError, Exception):
                    # Another committed claimant or a lost DB acknowledgement must keep its hold.
                    logger.warning("unsent_model_reservation_release_unavailable")
            raise
        if unsupported_continuation:
            raise ModelConnectionError("unsupported_model_continuation")
        if denied:
            raise ScopeViolation("model output unavailable")
        response = result.response
        if result.failure_code or response is None:
            raise ModelConnectionError(result.failure_code or "invalid_model_response")
        if (response.call_id, response.attempt_id, response.provider, response.model_id) != (
            logical_call_id,
            attempt_id,
            profile.provider.value,
            profile.model_id,
        ):
            raise ModelConnectionError("model_response_identity_mismatch")
        for call in response.tool_calls:
            arguments = OutputValidator.validate_tool_call(call, tools or ())
            definition = next(tool for tool in (tools or ()) if tool.name == call.name)
            validate_output_schema(arguments, definition.parameters_schema)
        if response_schema is not None:
            parsed = OutputValidator.validate_json_schema(response.content, response_schema)
            validate_output_schema(parsed, response_schema)
        final_response = response.model_copy(
            update={"charge_micros": charge, "continuation_ref": continuation}
        )
        if len(final_response.model_dump_json().encode("utf-8")) > 524288:
            raise ModelConnectionError("payload_too_large")
        async with transaction(self.factory) as session:
            await require_repository(session, org_id, principal_id, repository_id, write=True)
        return final_response

    async def _hold(
        self,
        org: UUID,
        repo: UUID,
        identity: UUID,
        profile_digest: str,
        evidence: ProviderUsageEvidenceV1 | None = None,
    ) -> None:
        try:
            async with asyncio.timeout(5):
                async with transaction(self.factory) as session:
                    await ModelBudgetService(session).hold_uncertain(org, identity)
                    state = await session.scalar(
                        select(model_reservations.c.state).where(
                            model_reservations.c.org_id == org,
                            model_reservations.c.reservation_id == identity,
                        )
                    )
                    if state == "uncertain_hold":
                        await record_usage_receipt(
                            session,
                            org,
                            repo,
                            identity,
                            profile_digest,
                            evidence
                            or ProviderUsageEvidenceV1(
                                quality="unknown", reason="missing_or_inconsistent_usage"
                            ),
                        )
        except (Exception, asyncio.CancelledError):
            logger.warning("model_uncertainty_persistence_failed")
