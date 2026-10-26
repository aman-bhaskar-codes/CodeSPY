"""Bedrock Converse wire codec; no AWS client construction or credential discovery."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from app.adapters.model.codec_support import (
    internal_tool_id,
    native_history,
    outcome,
    prepared,
    tool_call,
    validate_request,
    with_continuation,
)
from app.adapters.model.http_transport import ProviderTransportError
from app.contracts.model import ModelRequestV1, ModelRole, ToolCallV1
from app.contracts.model_configuration import ModelProfileV1
from app.contracts.model_transport import PreparedModelCallV1, ProviderOutcomeV1
from app.services.model_usage import normalize_usage


class BedrockConverseCodec:
    def prepare(self, request: ModelRequestV1, profile: ModelProfileV1) -> PreparedModelCallV1:
        if profile.endpoint_kind != "bedrock_converse":
            raise ValueError("codec endpoint does not match profile")
        validate_request(request, profile)
        if request.response_schema is not None:
            raise ValueError("native schema requires pinned SDK service-model qualification")
        system = []
        messages: list[dict[str, Any]] = []
        native_ids: dict[str, str] = {}
        for message in request.messages:
            if message.role == ModelRole.SYSTEM:
                system.append({"text": message.content})
                continue
            blocks: list[dict[str, Any]] = []
            if message.role == ModelRole.ASSISTANT and message.continuation_ref is not None:
                blocks = native_history(request, message.continuation_ref)
                native_calls = [item["toolUse"] for item in blocks if "toolUse" in item]
                if len(native_calls) != len(message.tool_calls):
                    raise ValueError("continuation call mismatch")
                for call, native in zip(message.tool_calls, native_calls, strict=True):
                    native_ids[call.id] = native["toolUseId"]
                messages.append({"role": "assistant", "content": blocks})
                continue
            if message.role == ModelRole.TOOL:
                if not message.tool_call_id:
                    raise ValueError("tool result requires identity")
                blocks.append(
                    {
                        "toolResult": {
                            "toolUseId": native_ids.get(message.tool_call_id, message.tool_call_id),
                            "content": [{"text": message.content}],
                        }
                    }
                )
            else:
                if message.content:
                    blocks.append({"text": message.content})
                for call in message.tool_calls:
                    blocks.append(
                        {
                            "toolUse": {
                                "toolUseId": call.id,
                                "name": call.name,
                                "input": json.loads(call.arguments_json),
                            }
                        }
                    )
            role = "assistant" if message.role == ModelRole.ASSISTANT else "user"
            if messages and messages[-1]["role"] == role:
                messages[-1]["content"].extend(blocks)
            else:
                messages.append({"role": role, "content": blocks})
        config: dict[str, Any] = {"maxTokens": request.max_tokens}
        if profile.temperature is not None:
            config["temperature"] = profile.temperature
        body: dict[str, Any] = {
            "modelId": request.model_id,
            "messages": messages,
            "inferenceConfig": config,
        }
        if profile.reasoning_mode == "bounded":
            if (
                profile.thinking_token_budget < 1024
                or profile.thinking_token_budget >= request.max_tokens
            ):
                raise ValueError("invalid Claude thinking budget")
            body["additionalModelRequestFields"] = {
                "thinking": {"type": "enabled", "budget_tokens": profile.thinking_token_budget}
            }
            config.pop("temperature", None)
        if system:
            body["system"] = system
        if request.tools and request.tool_choice != "none":
            body["toolConfig"] = {
                "tools": [
                    {
                        "toolSpec": {
                            "name": tool.name,
                            "description": tool.description,
                            "inputSchema": {"json": tool.parameters_schema},
                        }
                    }
                    for tool in request.tools
                ],
                "toolChoice": {"auto": {}}
                if request.tool_choice == "auto"
                else {"any": {}}
                if request.tool_choice == "required"
                else {"tool": {"name": request.tool_choice}},
            }
        return prepared(request, profile, body)

    def decode(
        self, request: ModelRequestV1, profile: ModelProfileV1, payload: dict[str, Any]
    ) -> ProviderOutcomeV1:
        raw = payload.get("usage", {})
        usage = normalize_usage("bedrock_claude", raw if isinstance(raw, dict) else {})
        if usage.usage is None:
            return ProviderOutcomeV1(usage=usage, failure_code="missing_usage")
        try:
            text = []
            calls: list[ToolCallV1] = []
            for block in payload["output"]["message"]["content"]:
                if set(block) == {"text"} and isinstance(block["text"], str):
                    text.append(block["text"])
                elif set(block) == {"toolUse"}:
                    tool = block["toolUse"]
                    calls.append(
                        tool_call(
                            internal_tool_id(tool.get("toolUseId"), request, len(calls)),
                            tool.get("name"),
                            tool.get("input"),
                            profile,
                        )
                    )
                elif set(block) == {"reasoningContent"}:
                    if not isinstance(block["reasoningContent"], dict):
                        raise ValueError("invalid reasoning block")
                else:
                    raise ValueError("unsupported content block")
            stop = {
                "end_turn": "stop",
                "stop_sequence": "stop",
                "tool_use": "tool_calls",
                "max_tokens": "length",
                "guardrail_intervened": "content_filter",
                "content_filtered": "content_filter",
            }[payload["stopReason"]]
            result = outcome(
                request,
                profile,
                usage,
                "".join(text),
                calls,
                stop,
                payload.get("ResponseMetadata", {}).get("RequestId"),
            )
            blocks = payload["output"]["message"]["content"]
            return (
                with_continuation(result, blocks)
                if calls or any("reasoningContent" in b for b in blocks)
                else result
            )
        except (ValueError, KeyError, TypeError):
            return ProviderOutcomeV1(usage=usage, failure_code="unsupported_protocol")


class AsyncConverseClientPort(Protocol):
    @property
    def retry_total_attempts(self) -> int: ...

    async def converse(self, **kwargs: Any) -> dict[str, Any]: ...


class ConfiguredAsyncConverseClient:
    """Validate an owner-created async SDK client without creating sessions or credentials."""

    retry_total_attempts = 1

    def __init__(self, client: Any) -> None:
        meta = client.meta
        if meta.region_name != "ap-south-1" or meta.config.retries.get("total_max_attempts") != 1:
            raise ValueError("Converse client requires Mumbai and exactly one total attempt")
        self._client = client

    async def converse(self, **kwargs: Any) -> dict[str, Any]:
        result: dict[str, Any] = await self._client.converse(**kwargs)
        return result


class BedrockModelTransport:
    """One bounded async Converse call using an explicitly injected client."""

    def __init__(self, client: AsyncConverseClientPort) -> None:
        if client.retry_total_attempts != 1:
            raise ValueError("Bedrock client must disable automatic retries")
        self.client = client
        self.codec = BedrockConverseCodec()
        self._capacity = asyncio.Semaphore(2)
        self._pending = 0

    @property
    def pending_calls(self) -> int:
        return self._pending

    def prepare(self, request: ModelRequestV1, profile: ModelProfileV1) -> PreparedModelCallV1:
        if profile.endpoint_kind != "bedrock_converse":
            raise ValueError("Converse endpoint required")
        return self.codec.prepare(request, profile)

    async def invoke(
        self,
        prepared: PreparedModelCallV1,
        *,
        before_dispatch: Callable[[], Awaitable[None]] | None = None,
    ) -> ProviderOutcomeV1:
        verified = self.prepare(prepared.request, prepared.profile)
        if verified.wire_digest != prepared.wire_digest or verified.body_json != prepared.body_json:
            raise ProviderTransportError("provider_request_identity_mismatch", dispatched=False)
        if self._pending >= 10:
            raise ProviderTransportError("provider_capacity_exceeded", dispatched=False)
        self._pending += 1
        sent = False
        try:
            async with asyncio.timeout(prepared.profile.timeouts.total_seconds):
                async with self._capacity:
                    if before_dispatch is not None:
                        await before_dispatch()
                    sent = True
                    payload = await self.client.converse(**json.loads(prepared.body_json))
                    if (
                        len(json.dumps(payload, allow_nan=False).encode())
                        > prepared.profile.response_byte_limit
                    ):
                        raise ProviderTransportError("provider_response_too_large")
                    return self.codec.decode(prepared.request, prepared.profile, payload)
        except TimeoutError:
            raise ProviderTransportError("provider_timeout", dispatched=sent) from None
        except ProviderTransportError:
            raise
        except Exception:
            if not sent:
                raise
            raise ProviderTransportError("provider_unavailable", dispatched=sent) from None
        finally:
            self._pending -= 1

    async def aclose(self) -> None:
        # The composition root owns the injected client's lifespan.
        return None
