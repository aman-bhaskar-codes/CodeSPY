"""Pure bounded composition through the same codecs used for provider dispatch."""

from dataclasses import dataclass

from app.contracts.context import ContextPacketV1
from app.contracts.context_manifest import (
    ContextBudgetBreakdownV1,
    MemoryReferenceV1,
    RequestContextLimitsV1,
)
from app.contracts.memory import MemoryEntryV1, MemoryState
from app.contracts.model import ChatMessageV1, ModelRequestV1, ModelRole
from app.contracts.model_configuration import ModelProfileV1
from app.contracts.model_transport import PreparedModelCallV1
from app.contracts.search import SearchScopeV1
from app.contracts.source_view import PrivacyPolicyV1
from app.domain.canonical_json import canonical_bytes, semantic_digest
from app.ports.model_transport import ProviderPreparationPort
from app.services.privacy_transform import transform_model_safe
from app.services.syntax_chunker import count_tokens


class ContextBudgetExceededError(ValueError):
    def __init__(self) -> None:
        super().__init__("context_budget_exceeded")


@dataclass(frozen=True)
class ComposedContext:
    prepared: PreparedModelCallV1
    selection_digest: str
    required_history_digest: str
    memory_references: tuple[MemoryReferenceV1, ...]
    budget: ContextBudgetBreakdownV1
    omissions: tuple[str, ...]


class ContextComposer:
    def compose(
        self,
        request: ModelRequestV1,
        profile: ModelProfileV1,
        codec: ProviderPreparationPort,
        *,
        memories: tuple[MemoryEntryV1, ...] = (),
        source: ContextPacketV1 | None = None,
        limits: RequestContextLimitsV1 | None = None,
        privacy: PrivacyPolicyV1 | None = None,
        scope: SearchScopeV1 | None = None,
        frozen_omissions: tuple[str, ...] = (),
    ) -> ComposedContext:
        limits = limits or RequestContextLimitsV1()
        privacy = privacy or PrivacyPolicyV1()
        if len(memories) > 10 or any(m.state != MemoryState.ACCEPTED for m in memories):
            raise ValueError("memory_not_eligible")
        if any(m.content_digest is None or m.expires_at is None for m in memories):
            raise ValueError("memory_not_eligible")
        if memories and scope is None:
            raise ValueError("bound context scope required")
        if scope is not None:
            if any(
                m.org_id != scope.org_id
                or m.repository_id != scope.repository_id
                or (not m.historical_hint and m.snapshot_id != scope.snapshot_id)
                for m in memories
            ):
                raise ValueError("memory_scope_mismatch")
            if source is not None and source.scope != scope:
                raise ValueError("source_scope_mismatch")
        if len({m.memory_id for m in memories}) != len(memories):
            raise ValueError("duplicate_memory")
        required = codec.prepare(request, profile)
        available = max(
            0, limits.max_input_tokens - count_tokens(required.body_json) - limits.safety_tokens
        )
        omissions: list[str] = list(frozen_omissions)
        selected: list[tuple[MemoryEntryV1, str]] = []
        memory_tokens = memory_bytes = historical = 0
        # Explicit trust labels travel with text as data, never in a system message.
        for memory in memories:
            safe = transform_model_safe(memory.summary.encode(), privacy).safe_text or ""
            block = canonical_bytes(
                {
                    "kind": "untrusted_repository_memory",
                    "id": memory.memory_id,
                    "version": memory.version,
                    "source_snapshot": memory.snapshot_id,
                    "historical_hint": memory.historical_hint,
                    "source_correspondence": memory.source_correspondence,
                    "evidence_pointers": [
                        p.model_dump(mode="json") for p in memory.evidence_pointers
                    ],
                    "topic": transform_model_safe(memory.topic.encode(), privacy).safe_text,
                    "summary": safe,
                }
            ).decode()
            tokens, size = count_tokens(block), len(block.encode())
            if (
                memory_tokens + tokens > min(1024, available // 4)
                or memory_bytes + size > 8192
                or (memory.historical_hint and historical >= 2)
            ):
                omissions.append("memory_budget")
                continue
            selected.append((memory, block))
            memory_tokens += tokens
            memory_bytes += size
            historical += int(memory.historical_hint)
        source_items = list(source.items) if source else []
        # Never rewrite caller history, native items or tool/result pairs.
        required_digest = semantic_digest(request.model_dump(mode="json"))
        for attempt in range(4):
            blocks = [block for _, block in selected]
            blocks += [
                canonical_bytes(
                    {
                        "kind": "untrusted_source_excerpt",
                        "citation": i.citation_key,
                        "digest": i.safe_content_sha256,
                        "content": i.safe_content,
                    }
                ).decode()
                for i in source_items
            ]
            messages = request.messages
            if blocks:
                data = ChatMessageV1(role=ModelRole.USER, content="\n".join(blocks))
                position = next(
                    (i for i, m in enumerate(messages) if m.role != ModelRole.SYSTEM), len(messages)
                )
                messages = (*messages[:position], data, *messages[position:])
            candidate = request.model_copy(update={"messages": messages})
            prepared = codec.prepare(candidate, profile)
            tokens = count_tokens(prepared.body_json)
            size = len(prepared.body_json.encode())
            conservative = size + 4096 + 64 * len(messages)
            fits = (
                tokens + limits.safety_tokens <= limits.max_input_tokens
                and size <= limits.max_wire_bytes
                and conservative + request.max_tokens + profile.thinking_token_budget
                <= profile.max_context_tokens
            )
            if fits:
                refs = tuple(
                    MemoryReferenceV1(
                        memory_id=m.memory_id,
                        version=m.version,
                        content_digest=m.content_digest or "",
                        historical_hint=m.historical_hint,
                        source_snapshot_id=m.snapshot_id,
                        source_correspondence=m.source_correspondence,
                    )
                    for m, _ in selected
                )
                budget = ContextBudgetBreakdownV1(
                    input_tokens=tokens,
                    wire_bytes=size,
                    system_tokens=sum(
                        count_tokens(m.content)
                        for m in request.messages
                        if m.role == ModelRole.SYSTEM
                    ),
                    tool_schema_tokens=count_tokens(
                        canonical_bytes([t.model_dump(mode="json") for t in request.tools]).decode()
                    ),
                    required_history_tokens=sum(
                        count_tokens(m.content)
                        for m in request.messages
                        if m.role != ModelRole.SYSTEM
                    ),
                    source_tokens=sum(count_tokens(i.safe_content) for i in source_items),
                    memory_tokens=sum(count_tokens(b) for _, b in selected),
                    output_reserve_tokens=request.max_tokens + profile.thinking_token_budget,
                    safety_tokens=limits.safety_tokens,
                )
                digest = semantic_digest(
                    {
                        "strategy": "reviewed-context-v1",
                        "required": required_digest,
                        "profile": profile.model_dump(mode="json"),
                        "privacy": privacy.model_dump(mode="json"),
                        "limits": limits.model_dump(),
                        "memories": [r.model_dump(mode="json") for r in refs],
                        "source": source.packet_sha256 if source else None,
                        "source_items": [i.chunk_id for i in source_items],
                        "omissions": omissions,
                        "wire": prepared.wire_digest,
                    }
                )
                return ComposedContext(
                    prepared, digest, required_digest, refs, budget, tuple(omissions)
                )
            if attempt == 0:
                selected = [(m, b) for m, b in selected if not m.historical_hint]
                omissions.append("historical_context_pressure")
            elif attempt == 1:
                extra_tokens = max(0, tokens + limits.safety_tokens - limits.max_input_tokens)
                extra_bytes = max(
                    0,
                    size - limits.max_wire_bytes,
                    conservative
                    + request.max_tokens
                    + profile.thinking_token_budget
                    - profile.max_context_tokens,
                )
                while selected and (extra_tokens > 0 or extra_bytes > 0):
                    _, removed = selected.pop()
                    extra_tokens -= count_tokens(removed)
                    extra_bytes -= len(removed.encode())
                omissions.append("memory_context_pressure")
            else:
                source_items = []
                omissions.append("source_context_pressure")
        raise ContextBudgetExceededError()
