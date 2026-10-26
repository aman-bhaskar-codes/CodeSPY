from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from app.adapters.model.bedrock import BedrockConverseCodec
from app.adapters.model.gemini import GeminiContentCodec
from app.adapters.model.openai import OpenAIResponsesCodec
from app.contracts.memory import MemoryEntryV1, MemoryKind, MemoryState
from app.contracts.search import SearchScopeV1
from tests.model_connections.unit.test_codecs import request
from tests.model_connections.unit.test_registry import profile


def memory(**changes):
    now = datetime.now(UTC)
    return MemoryEntryV1(
        **(
            {
                "memory_id": uuid4(),
                "org_id": uuid4(),
                "repository_id": uuid4(),
                "snapshot_id": uuid4(),
                "memory_kind": MemoryKind.ARCHITECTURE_FACT,
                "topic": "routing",
                "summary": "Scoped adapters",
                "created_at": now,
                "updated_at": now,
                "state": MemoryState.ACCEPTED,
                "version": 2,
                "content_digest": "a" * 64,
                "expires_at": now + timedelta(days=90),
            }
            | changes
        )
    )


@pytest.mark.parametrize(
    "alias,codec",
    [
        ("openai_fast", OpenAIResponsesCodec),
        ("claude_fast", BedrockConverseCodec),
        ("gemini_general", GeminiContentCodec),
    ],
)
def test_whole_request_composition_is_stable_and_keeps_required_history(alias, codec):
    from app.services.context_composition import ContextComposer

    p = profile(alias)
    base = request(p)
    item = memory()
    composer = ContextComposer()
    scope = SearchScopeV1(
        org_id=item.org_id,
        repository_id=item.repository_id,
        snapshot_id=item.snapshot_id,
        principal_id=uuid4(),
    )
    a = composer.compose(base, p, codec(), memories=(item,), scope=scope)
    b = composer.compose(base, p, codec(), memories=(item,), scope=scope)
    assert a.selection_digest == b.selection_digest
    assert a.prepared.wire_digest == b.prepared.wire_digest
    assert a.prepared.request.messages[0] == base.messages[0]
    assert a.prepared.request.messages[-1] == base.messages[-1]
    assert a.budget.input_tokens <= 4096
    assert a.budget.wire_bytes <= 32768
    assert "untrusted" in a.prepared.body_json.lower()
    assert item.summary not in a.prepared.request.messages[0].content
    changed = composer.compose(
        base, p, codec(), memories=(item.model_copy(update={"version": 3}),), scope=scope
    )
    assert changed.selection_digest != a.selection_digest


def test_candidates_cannot_enter_context():
    from app.services.context_composition import ContextComposer

    p = profile("openai_fast")
    with pytest.raises(ValueError, match="memory_not_eligible"):
        ContextComposer().compose(
            request(p), p, OpenAIResponsesCodec(), memories=(memory(state=MemoryState.CANDIDATE),)
        )


def test_required_history_overflow_fails_without_dropping_messages():
    from app.services.context_composition import ContextBudgetExceededError, ContextComposer

    p = profile("openai_fast")
    base = request(p)
    base = base.model_copy(update={"opaque_context_json": '{"retained":"' + ("x" * 40000) + '"}'})
    # Codec consumes only protocol-referenced opaque items; required user text always counts.
    base = base.model_copy(
        update={
            "messages": (
                base.messages[0],
                base.messages[-1].model_copy(update={"content": "x " * 10000}),
            )
        }
    )
    with pytest.raises(ContextBudgetExceededError):
        ContextComposer().compose(base, p, OpenAIResponsesCodec())


def test_compaction_replay_preserves_the_original_omission_decision():
    from app.services.context_composition import ContextComposer

    p = profile("openai_fast")
    base = request(p)
    item = memory(summary=" repository scope" * 230)
    scope = SearchScopeV1(
        org_id=item.org_id,
        repository_id=item.repository_id,
        snapshot_id=item.snapshot_id,
        principal_id=uuid4(),
    )
    composer = ContextComposer()
    first = composer.compose(base, p, OpenAIResponsesCodec(), memories=(item,), scope=scope)
    assert not first.memory_references
    replay = composer.compose(
        base, p, OpenAIResponsesCodec(), scope=scope, frozen_omissions=first.omissions
    )
    assert first.selection_digest == replay.selection_digest
    assert first.prepared == replay.prepared
