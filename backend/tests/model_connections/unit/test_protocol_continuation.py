"""Opaque signed items round-trip without exposing them as tool arguments."""

import json
from uuid import uuid4

import pytest
from app.adapters.model.bedrock import BedrockConverseCodec
from app.adapters.model.gemini import GeminiContentCodec
from app.adapters.model.openai import OpenAIResponsesCodec
from app.contracts.model import ChatMessageV1, ModelRole

from tests.model_connections.unit.test_codecs import request
from tests.model_connections.unit.test_registry import profile


@pytest.mark.parametrize(
    "alias,codec,payload",
    [
        (
            "openai_reasoning",
            OpenAIResponsesCodec,
            {
                "status": "completed",
                "output": [
                    {
                        "type": "reasoning",
                        "id": "opaque-reason",
                        "encrypted_content": "opaque-canary",
                    },
                    {
                        "type": "function_call",
                        "call_id": "long_" + "x" * 100,
                        "name": "read_source",
                        "arguments": "{}",
                    },
                ],
                "usage": {
                    "input_tokens": 10,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens": 5,
                    "output_tokens_details": {"reasoning_tokens": 2},
                },
            },
        ),
        (
            "claude_reasoning",
            BedrockConverseCodec,
            {
                "output": {
                    "message": {
                        "content": [
                            {
                                "reasoningContent": {
                                    "reasoningText": {
                                        "text": "private-reason",
                                        "signature": "opaque-canary",
                                    }
                                }
                            },
                            {
                                "toolUse": {
                                    "toolUseId": "long_" + "x" * 100,
                                    "name": "read_source",
                                    "input": {},
                                }
                            },
                        ]
                    }
                },
                "stopReason": "tool_use",
                "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
            },
        ),
        (
            "gemini_general",
            GeminiContentCodec,
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "functionCall": {"name": "read_source", "args": {}},
                                    "thoughtSignature": "opaque-canary",
                                }
                            ]
                        },
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 10,
                    "candidatesTokenCount": 5,
                    "thoughtsTokenCount": 2,
                },
            },
        ),
    ],
)
def test_required_native_items_survive_next_tool_turn(alias, codec, payload):
    p = profile(alias, reasoning_mode="provider_default")
    req = request(p)
    result = codec().decode(req, p, payload)
    assert result.response is not None
    assert result.continuation_json is not None
    assert "opaque-canary" in result.continuation_json
    call = result.response.tool_calls[0]
    assert len(call.id) <= 64
    ref = uuid4()
    next_req = request(
        p,
        messages=(
            ChatMessageV1(role=ModelRole.USER, content="synthetic"),
            ChatMessageV1(role=ModelRole.ASSISTANT, tool_calls=(call,), continuation_ref=ref),
            ChatMessageV1(role=ModelRole.TOOL, tool_call_id=call.id, content="tool result"),
        ),
        opaque_context_json=json.dumps({str(ref): json.loads(result.continuation_json)}),
    )
    prepared = codec().prepare(next_req, p)
    assert "opaque-canary" in prepared.body_json
    assert call.arguments_json == "{}"


def test_unsigned_openai_tool_id_can_still_be_short():
    p = profile()
    assert p.reasoning_mode == "none"


def test_invalid_model_output_is_not_copied_into_debug_logs(caplog):
    import logging

    from app.services.short_path_investigator import ShortPathInvestigator

    caplog.set_level(logging.DEBUG)
    assert (
        object.__new__(ShortPathInvestigator)._extract_verdict_payload("private-output-canary")
        is None
    )
    assert "private-output-canary" not in caplog.text
