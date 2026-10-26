"""Offline-testable OpenAI Responses encoding and numeric evidence decoding."""

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


class OpenAIResponsesCodec:
    def prepare(self, request: ModelRequestV1, profile: ModelProfileV1) -> PreparedModelCallV1:
        if profile.endpoint_kind != "openai_responses":
            raise ValueError("codec endpoint does not match profile")
        validate_request(request, profile)
        items: list[dict[str, Any]] = []
        native_ids: dict[str, str] = {}
        for message in request.messages:
            if message.role == ModelRole.ASSISTANT and message.continuation_ref is not None:
                blocks = native_history(request, message.continuation_ref)
                native_calls = [item for item in blocks if item.get("type") == "function_call"]
                if len(native_calls) != len(message.tool_calls):
                    raise ValueError("continuation call mismatch")
                for call, native in zip(message.tool_calls, native_calls, strict=True):
                    native_ids[call.id] = native["call_id"]
                items.extend(blocks)
                continue
            if message.role == ModelRole.TOOL:
                if not message.tool_call_id:
                    raise ValueError("tool result requires call identity")
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": native_ids.get(message.tool_call_id, message.tool_call_id),
                        "output": message.content,
                    }
                )
                continue
            if message.content:
                items.append({"role": message.role.value, "content": message.content})
            for call in message.tool_calls:
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call.id,
                        "name": call.name,
                        "arguments": call.arguments_json,
                    }
                )
        body: dict[str, Any] = {
            "model": request.model_id,
            "input": items,
            "max_output_tokens": request.max_tokens,
            "store": False,
        }
        if profile.reasoning_mode != "none":
            body["include"] = ["reasoning.encrypted_content"]
        if profile.temperature is not None:
            body["temperature"] = profile.temperature
        if request.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters_schema,
                    "strict": False,
                }
                for tool in request.tools
            ]
            body["tool_choice"] = (
                request.tool_choice
                if request.tool_choice in ("auto", "none", "required")
                else {"type": "function", "name": request.tool_choice}
            )
        if request.response_schema is not None:
            body["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "codespy_result",
                    "strict": True,
                    "schema": request.response_schema,
                }
            }
        return prepared(request, profile, body)

    def decode(
        self, request: ModelRequestV1, profile: ModelProfileV1, payload: dict[str, Any]
    ) -> ProviderOutcomeV1:
        usage = normalize_usage(
            "openai", payload.get("usage", {}) if isinstance(payload.get("usage", {}), dict) else {}
        )
        if usage.usage is None:
            return ProviderOutcomeV1(usage=usage, failure_code="missing_usage")
        try:
            if payload.get("model", request.model_id) != request.model_id:
                raise ValueError("unqualified returned model alias")
            text: list[str] = []
            calls: list[ToolCallV1] = []
            refusal = False
            for item in payload.get("output", []):
                if item["type"] == "message":
                    for part in item["content"]:
                        if part["type"] == "output_text":
                            if not isinstance(part["text"], str):
                                raise ValueError("invalid text")
                            text.append(part["text"])
                        elif part["type"] == "refusal":
                            refusal = True
                        else:
                            raise ValueError("unsupported content block")
                elif item["type"] == "function_call":
                    calls.append(
                        tool_call(
                            internal_tool_id(item.get("call_id"), request, len(calls)),
                            item.get("name"),
                            item.get("arguments"),
                            profile,
                        )
                    )
                elif item["type"] == "reasoning":
                    if not isinstance(item.get("encrypted_content"), str):
                        raise ValueError("encrypted reasoning continuation required")
                else:
                    raise ValueError("unsupported output item")
            status = payload.get("status")
            if status not in ("completed", "incomplete"):
                raise ValueError("response is not terminal")
            finish = "content_filter" if refusal else "tool_calls" if calls else "stop"
            if status == "incomplete":
                reason = payload.get("incomplete_details", {}).get("reason")
                if reason == "max_output_tokens":
                    finish = "length"
                elif reason == "content_filter":
                    finish = "content_filter"
                else:
                    raise ValueError("unsupported incomplete reason")
            result = outcome(
                request, profile, usage, "".join(text), calls, finish, payload.get("id")
            )
            return (
                with_continuation(result, payload["output"])
                if calls
                or any(item.get("type") == "reasoning" for item in payload.get("output", []))
                else result
            )
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            return ProviderOutcomeV1(usage=usage, failure_code="unsupported_protocol")
