"""Provider-shaped offline requests and responses; no vendor credentials."""

import json
from uuid import uuid4

import pytest
from app.adapters.model.bedrock import BedrockConverseCodec
from app.adapters.model.gemini import GeminiContentCodec
from app.adapters.model.openai import OpenAIResponsesCodec
from app.contracts.model import ChatMessageV1, ModelRequestV1, ModelRole, ToolDefinitionV1

from tests.model_connections.unit.test_registry import profile


def request(p, **changes):
    return ModelRequestV1(
        **{
            **{
                "investigation_id": uuid4(),
                "call_id": uuid4(),
                "attempt_id": 1,
                "provider": p.provider.value,
                "model_id": p.model_id,
                "reservation_id": uuid4(),
                "max_tokens": 100,
                "messages": (
                    ChatMessageV1(role=ModelRole.SYSTEM, content="system fixture"),
                    ChatMessageV1(role=ModelRole.USER, content="synthetic request"),
                ),
            },
            **changes,
        }
    )


@pytest.mark.parametrize(
    "alias,codec",
    [
        ("openai_fast", OpenAIResponsesCodec),
        ("claude_fast", BedrockConverseCodec),
        ("gemini_general", GeminiContentCodec),
    ],
)
def test_provider_request_is_bounded_and_uses_explicit_output_limit(alias, codec):
    p = profile(alias)
    prepared = codec().prepare(request(p), p)
    body = json.loads(prepared.body_json)
    assert prepared.profile == p
    assert "temperature" not in body
    if alias.startswith("openai"):
        assert body["store"] is False
        assert body["max_output_tokens"] == 100
    elif alias.startswith("claude"):
        assert body["inferenceConfig"]["maxTokens"] == 100
        assert body["system"][0]["text"] == "system fixture"
    else:
        assert body["generationConfig"]["maxOutputTokens"] == 100
        assert body["generationConfig"]["candidateCount"] == 1
        assert body["systemInstruction"]["parts"][0]["text"] == "system fixture"


@pytest.mark.parametrize(
    "alias,codec",
    [
        ("openai_fast", OpenAIResponsesCodec),
        ("claude_fast", BedrockConverseCodec),
        ("gemini_general", GeminiContentCodec),
    ],
)
def test_request_identity_mismatch_rejected_before_network(alias, codec):
    p = profile(alias)
    with pytest.raises(ValueError):
        codec().prepare(request(p, model_id="other-model"), p)


@pytest.mark.parametrize(
    "alias,codec,payload",
    [
        (
            "openai_fast",
            OpenAIResponsesCodec,
            {
                "id": "resp_fixture",
                "model": "synthetic-openai_fast",
                "status": "completed",
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": "answer"}]}
                ],
                "usage": {
                    "input_tokens": 10,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens": 5,
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            },
        ),
        (
            "claude_fast",
            BedrockConverseCodec,
            {
                "output": {"message": {"role": "assistant", "content": [{"text": "answer"}]}},
                "stopReason": "end_turn",
                "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
            },
        ),
        (
            "gemini_general",
            GeminiContentCodec,
            {
                "candidates": [
                    {
                        "content": {"role": "model", "parts": [{"text": "answer"}]},
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 10,
                    "candidatesTokenCount": 5,
                    "thoughtsTokenCount": 0,
                    "totalTokenCount": 15,
                },
            },
        ),
    ],
)
def test_provider_response_normalizes_usage_without_claiming_adapter_charge(alias, codec, payload):
    p = profile(alias)
    req = request(p)
    outcome = codec().decode(req, p, payload)
    assert outcome.response.content == "answer"
    assert outcome.response.charge_micros == 0
    assert outcome.usage.usage.input_tokens == 10
    assert outcome.response.call_id == req.call_id


@pytest.mark.parametrize(
    "alias,codec",
    [
        ("openai_fast", OpenAIResponsesCodec),
        ("claude_fast", BedrockConverseCodec),
        ("gemini_general", GeminiContentCodec),
    ],
)
def test_missing_usage_does_not_become_free_response(alias, codec):
    p = profile(alias)
    outcome = codec().decode(request(p), p, {})
    assert outcome.response is None
    assert outcome.usage.quality == "unknown"


def test_unsupported_schema_keyword_rejected_before_request():
    p = profile()
    with pytest.raises(ValueError):
        OpenAIResponsesCodec().prepare(
            request(p, response_schema={"$ref": "https://untrusted.invalid/schema"}), p
        )


def test_models_propose_tools_without_executing_them():
    p = profile()
    req = request(
        p,
        tools=(
            ToolDefinitionV1(
                name="read_source",
                description="Read fixture source",
                parameters_schema={"type": "object"},
            ),
        ),
    )
    prepared = OpenAIResponsesCodec().prepare(req, p)
    assert json.loads(prepared.body_json)["tools"][0]["name"] == "read_source"
    outcome = OpenAIResponsesCodec().decode(
        req,
        p,
        {
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "read_source",
                    "arguments": "{}",
                }
            ],
            "usage": {
                "input_tokens": 10,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": 5,
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        },
    )
    assert outcome.response.tool_calls[0].name == "read_source"
    assert outcome.response.finish_reason == "tool_calls"


@pytest.mark.parametrize("codec", [OpenAIResponsesCodec, BedrockConverseCodec, GeminiContentCodec])
def test_disabled_profile_rejected_by_codec(codec):
    alias = {
        OpenAIResponsesCodec: "openai_fast",
        BedrockConverseCodec: "claude_fast",
        GeminiContentCodec: "gemini_general",
    }[codec]
    p = profile(alias, enabled=False)
    with pytest.raises(ValueError):
        codec().prepare(request(p), p)


def test_malformed_output_keeps_known_usage_for_settlement():
    p = profile()
    result = OpenAIResponsesCodec().decode(
        request(p),
        p,
        {
            "status": "completed",
            "output": [{"type": "message", "content": [123]}],
            "usage": {
                "input_tokens": 10,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": 5,
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        },
    )
    assert result.response is None
    assert result.usage.quality == "measured"
    assert result.failure_code == "unsupported_protocol"


@pytest.mark.parametrize(
    "codec,alias",
    [
        (OpenAIResponsesCodec, "gemini_general"),
        (BedrockConverseCodec, "openai_fast"),
        (GeminiContentCodec, "claude_fast"),
    ],
)
def test_codec_rejects_another_provider_endpoint(codec, alias):
    p = profile(alias)
    with pytest.raises(ValueError):
        codec().prepare(request(p), p)


def test_existing_investigator_tool_contracts_prepare_without_rewriting_constraints():
    from app.services.investigator_tools import STANDARD_INVESTIGATOR_TOOLS

    p = profile()
    result = OpenAIResponsesCodec().prepare(request(p, tools=STANDARD_INVESTIGATOR_TOOLS), p)
    body = json.loads(result.body_json)
    assert body["tools"][0]["parameters"]["properties"]["limit"]["default"] == 5
    assert body["tools"][0]["strict"] is False


def test_local_output_validation_enforces_nested_types_and_extra_fields():
    from app.adapters.model.codec_support import validate_output_schema
    from app.services.output_validator import SchemaMismatchError

    schema = {
        "type": "object",
        "properties": {"count": {"type": "integer"}},
        "required": ["count"],
        "additionalProperties": False,
    }
    validate_output_schema({"count": 2}, schema)
    for value in ({"count": True}, {"count": "2"}, {"count": 2, "extra": 1}):
        with pytest.raises(SchemaMismatchError):
            validate_output_schema(value, schema)


def test_gemini_no_reasoning_policy_sends_explicit_zero_thinking_budget():
    p = profile("gemini_general")
    body = json.loads(GeminiContentCodec().prepare(request(p), p).body_json)
    assert body["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}


def test_openai_does_not_silently_ignore_unsupported_thinking_token_budget():
    p = profile("openai_reasoning", reasoning_mode="bounded", thinking_token_budget=1024)
    with pytest.raises(ValueError):
        OpenAIResponsesCodec().prepare(request(p), p)
