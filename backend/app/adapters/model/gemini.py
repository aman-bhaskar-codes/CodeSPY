"""Direct Gemini generateContent codec with explicit tool proposals only."""

import json
from typing import Any

from app.adapters.model.codec_support import (
    internal_tool_id,
    native_history,
    outcome,
    prepared,
    tool_call,
    validate_request,
    with_continuation,
)
from app.contracts.model import ModelRequestV1, ModelRole, ToolCallV1
from app.contracts.model_configuration import ModelProfileV1
from app.contracts.model_transport import PreparedModelCallV1, ProviderOutcomeV1
from app.services.model_usage import normalize_usage


class GeminiContentCodec:
    def prepare(self, request: ModelRequestV1, profile: ModelProfileV1) -> PreparedModelCallV1:
        if profile.endpoint_kind != "gemini_generate_content":
            raise ValueError("codec endpoint does not match profile")
        validate_request(request, profile)
        system = []
        contents: list[dict[str, Any]] = []
        names: dict[str, str] = {}
        native_ids: dict[str, str | None] = {}
        for message in request.messages:
            if message.role == ModelRole.SYSTEM:
                system.append({"text": message.content})
                continue
            parts: list[dict[str, Any]] = []
            if message.role == ModelRole.ASSISTANT and message.continuation_ref is not None:
                parts = native_history(request, message.continuation_ref)
                native_calls = [part["functionCall"] for part in parts if "functionCall" in part]
                if len(native_calls) != len(message.tool_calls):
                    raise ValueError("continuation call mismatch")
                for call, native in zip(message.tool_calls, native_calls, strict=True):
                    names[call.id] = call.name
                    native_ids[call.id] = native.get("id")
                contents.append({"role": "model", "parts": parts})
                continue
            if message.role == ModelRole.TOOL:
                if message.tool_call_id is None:
                    raise ValueError("tool result requires call identity")
                name = names.get(message.tool_call_id)
                if name is None:
                    raise ValueError("unpaired tool result")
                parts.append(
                    {
                        "functionResponse": {
                            "id": native_ids.get(message.tool_call_id, message.tool_call_id),
                            "name": name,
                            "response": {"result": message.content},
                        }
                    }
                )
            else:
                if message.content:
                    parts.append({"text": message.content})
                for call in message.tool_calls:
                    names[call.id] = call.name
                    parts.append(
                        {
                            "functionCall": {
                                "id": call.id,
                                "name": call.name,
                                "args": json.loads(call.arguments_json),
                            }
                        }
                    )
            contents.append(
                {"role": "model" if message.role == ModelRole.ASSISTANT else "user", "parts": parts}
            )
        config: dict[str, Any] = {"maxOutputTokens": request.max_tokens, "candidateCount": 1}
        for item in contents:
            for part in item["parts"]:
                if "functionResponse" in part and part["functionResponse"].get("id") is None:
                    part["functionResponse"].pop("id", None)
        if profile.reasoning_mode in ("none", "bounded"):
            config["thinkingConfig"] = {"thinkingBudget": profile.thinking_token_budget}
        if profile.temperature is not None:
            config["temperature"] = profile.temperature
        if request.response_schema is not None:
            config.update(
                responseMimeType="application/json", responseJsonSchema=request.response_schema
            )
        body: dict[str, Any] = {"contents": contents, "generationConfig": config}
        if system:
            body["systemInstruction"] = {"parts": system}
        if request.tools:
            body["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": tool.parameters_schema,
                        }
                        for tool in request.tools
                    ]
                }
            ]
            body["toolConfig"] = {
                "functionCallingConfig": {
                    "mode": {"auto": "AUTO", "none": "NONE", "required": "ANY"}.get(
                        request.tool_choice, "ANY"
                    )
                }
            }
            if request.tool_choice not in ("auto", "none", "required"):
                body["toolConfig"]["functionCallingConfig"]["allowedFunctionNames"] = [
                    request.tool_choice
                ]
        return prepared(request, profile, body)

    def decode(
        self, request: ModelRequestV1, profile: ModelProfileV1, payload: dict[str, Any]
    ) -> ProviderOutcomeV1:
        raw = payload.get("usageMetadata", {})
        usage = normalize_usage(
            "google_gemini",
            raw if isinstance(raw, dict) else {},
            reasoning_enabled=profile.reasoning_mode != "none",
        )
        if usage.usage is None:
            return ProviderOutcomeV1(usage=usage, failure_code="missing_usage")
        try:
            candidates = payload["candidates"]
            if len(candidates) != 1:
                raise ValueError("one candidate required")
            candidate = candidates[0]
            text = []
            calls: list[ToolCallV1] = []
            for part in candidate.get("content", {}).get("parts", []):
                if part.get("thought"):
                    continue
                if set(part).issubset({"text", "thoughtSignature"}) and isinstance(
                    part.get("text"), str
                ):
                    text.append(part["text"])
                elif "functionCall" in part and set(part).issubset(
                    {"functionCall", "thoughtSignature"}
                ):
                    call = part["functionCall"]
                    calls.append(
                        tool_call(
                            internal_tool_id(call.get("id"), request, len(calls)),
                            call.get("name"),
                            call.get("args"),
                            profile,
                        )
                    )
                else:
                    raise ValueError("unsupported content part")
            finish = {
                "STOP": "tool_calls" if calls else "stop",
                "MAX_TOKENS": "length",
                "SAFETY": "content_filter",
                "RECITATION": "content_filter",
                "BLOCKLIST": "content_filter",
                "PROHIBITED_CONTENT": "content_filter",
            }[candidate["finishReason"]]
            result = outcome(
                request, profile, usage, "".join(text), calls, finish, payload.get("responseId")
            )
            parts = candidate.get("content", {}).get("parts", [])
            return (
                with_continuation(result, parts)
                if calls or any("thoughtSignature" in part or part.get("thought") for part in parts)
                else result
            )
        except (ValueError, KeyError, TypeError):
            return ProviderOutcomeV1(usage=usage, failure_code="unsupported_protocol")
