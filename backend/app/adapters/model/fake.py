"""Deterministic, offline fake model adapter for testing and simulation."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from enum import StrEnum
from typing import Any
from uuid import uuid4

from app.contracts.model import (
    ModelRequestV1,
    ModelResponseV1,
    ModelStreamChunkV1,
    TokenUsageV1,
    ToolCallV1,
)
from app.ports.model import ModelPort


class FakeFailureMode(StrEnum):
    NONE = "none"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    MALFORMED_JSON = "malformed_json"
    TOOL_CALL_SCHEMA_MISMATCH = "tool_call_schema_mismatch"
    AMBIGUOUS_COMPLETION = "ambiguous_completion"
    PROVIDER_REFUSAL = "provider_refusal"
    SERVER_ERROR = "server_error"


class FakeModelAdapterError(Exception):
    """Base exception for fake model adapter failures."""


class FakeModelTimeoutError(FakeModelAdapterError):
    """Simulated provider timeout."""


class FakeModelRateLimitError(FakeModelAdapterError):
    """Simulated rate limit exceeded."""


class FakeModelRefusalError(FakeModelAdapterError):
    """Simulated safety refusal."""


class FakeModelAdapter(ModelPort):
    """Deterministic offline model adapter with failure injection and replay capabilities."""

    def __init__(
        self,
        default_content: str = "This is a deterministic fake response.",
        failure_mode: FakeFailureMode = FakeFailureMode.NONE,
        canned_responses: list[ModelResponseV1 | str] | None = None,
        custom_handler: Callable[[ModelRequestV1], ModelResponseV1] | None = None,
        latency_seconds: float = 0.0,
        fixed_input_tokens: int = 50,
        fixed_output_tokens: int = 30,
        fixed_thinking_tokens: int = 0,
    ) -> None:
        self.default_content = default_content
        self.failure_mode = failure_mode
        self.canned_responses: list[ModelResponseV1 | str] = list(canned_responses or [])
        self.custom_handler = custom_handler
        self.latency_seconds = latency_seconds
        self.fixed_input_tokens = fixed_input_tokens
        self.fixed_output_tokens = fixed_output_tokens
        self.fixed_thinking_tokens = fixed_thinking_tokens
        self.call_history: list[ModelRequestV1] = []

    def set_failure_mode(self, mode: FakeFailureMode) -> None:
        self.failure_mode = mode

    def queue_response(self, response: ModelResponseV1 | str) -> None:
        self.canned_responses.append(response)

    async def generate(self, request: ModelRequestV1) -> ModelResponseV1:
        self.call_history.append(request)

        if self.latency_seconds > 0:
            await asyncio.sleep(self.latency_seconds)

        # Trigger injected failures
        if self.failure_mode == FakeFailureMode.TIMEOUT:
            raise FakeModelTimeoutError("Simulated timeout connecting to model provider")
        if self.failure_mode == FakeFailureMode.RATE_LIMIT:
            raise FakeModelRateLimitError("Simulated provider rate limit exceeded (429)")
        if self.failure_mode == FakeFailureMode.PROVIDER_REFUSAL:
            raise FakeModelRefusalError("Simulated provider safety refusal")
        if self.failure_mode == FakeFailureMode.SERVER_ERROR:
            raise FakeModelAdapterError("Simulated internal provider server error (500)")
        if self.failure_mode == FakeFailureMode.AMBIGUOUS_COMPLETION:
            # Simulates network connection dropping before client receipt
            raise FakeModelTimeoutError("Connection dropped during response transmission")

        # Custom handler override
        if self.custom_handler:
            return self.custom_handler(request)

        # Canned responses queue
        if self.canned_responses:
            next_resp = self.canned_responses.pop(0)
            if isinstance(next_resp, ModelResponseV1):
                return next_resp
            return ModelResponseV1(
                call_id=request.call_id,
                attempt_id=request.attempt_id,
                provider=request.provider,
                model_id=request.model_id,
                content=next_resp,
                usage=TokenUsageV1(
                    input_tokens=self.fixed_input_tokens,
                    output_tokens=self.fixed_output_tokens,
                    thinking_tokens=self.fixed_thinking_tokens,
                ),
                charge_micros=0,
                finish_reason="stop",
                provider_request_id=f"fake-{uuid4().hex[:12]}",
            )

        # Failure mode: Malformed JSON when structured output was requested
        if self.failure_mode == FakeFailureMode.MALFORMED_JSON:
            return ModelResponseV1(
                call_id=request.call_id,
                attempt_id=request.attempt_id,
                provider=request.provider,
                model_id=request.model_id,
                content='{"incomplete_json": true, "unclosed_string": "',
                usage=TokenUsageV1(
                    input_tokens=self.fixed_input_tokens,
                    output_tokens=self.fixed_output_tokens,
                ),
                charge_micros=0,
                finish_reason="stop",
                provider_request_id=f"fake-{uuid4().hex[:12]}",
            )

        # Failure mode: Tool schema mismatch
        if self.failure_mode == FakeFailureMode.TOOL_CALL_SCHEMA_MISMATCH and request.tools:
            tool = request.tools[0]
            bad_call = ToolCallV1(
                id=f"call_{uuid4().hex[:8]}",
                name=tool.name,
                arguments_json='{"invalid_param_not_in_schema": 999}',
            )
            return ModelResponseV1(
                call_id=request.call_id,
                attempt_id=request.attempt_id,
                provider=request.provider,
                model_id=request.model_id,
                content="",
                tool_calls=(bad_call,),
                usage=TokenUsageV1(
                    input_tokens=self.fixed_input_tokens,
                    output_tokens=self.fixed_output_tokens,
                ),
                charge_micros=0,
                finish_reason="tool_calls",
                provider_request_id=f"fake-{uuid4().hex[:12]}",
            )

        # Handle structured output generation if schema is provided and not custom configured
        content = self.default_content
        is_default = self.default_content == "This is a deterministic fake response."
        if request.response_schema and is_default:
            content = self._generate_schema_mock(request.response_schema)

        return ModelResponseV1(
            call_id=request.call_id,
            attempt_id=request.attempt_id,
            provider=request.provider,
            model_id=request.model_id,
            content=content,
            usage=TokenUsageV1(
                input_tokens=self.fixed_input_tokens,
                output_tokens=self.fixed_output_tokens,
                thinking_tokens=self.fixed_thinking_tokens,
            ),
            charge_micros=0,
            finish_reason="stop",
            provider_request_id=f"fake-{uuid4().hex[:12]}",
        )

    async def stream(self, request: ModelRequestV1) -> AsyncIterator[ModelStreamChunkV1]:
        response = await self.generate(request)
        words = response.content.split()
        for i, word in enumerate(words):
            if self.latency_seconds > 0:
                await asyncio.sleep(self.latency_seconds / len(words))
            yield ModelStreamChunkV1(
                call_id=request.call_id,
                delta_text=word + (" " if i < len(words) - 1 else ""),
                is_final=False,
            )

        yield ModelStreamChunkV1(
            call_id=request.call_id,
            delta_text="",
            is_final=True,
            usage=response.usage,
            finish_reason=response.finish_reason,
        )

    def _generate_schema_mock(self, schema: dict[str, Any]) -> str:
        """Produce mock JSON adhering to standard json schema fields."""
        properties = schema.get("properties", {})
        result: dict[str, Any] = {}
        for prop, spec in properties.items():
            ptype = spec.get("type", "string")
            if ptype == "string":
                if "enum" in spec:
                    result[prop] = spec["enum"][0]
                else:
                    result[prop] = f"sample_{prop}"
            elif ptype == "integer":
                result[prop] = 1
            elif ptype == "number":
                result[prop] = 1.0
            elif ptype == "boolean":
                result[prop] = True
            elif ptype == "array":
                result[prop] = []
            elif ptype == "object":
                result[prop] = {}
            else:
                result[prop] = None
        return json.dumps(result)
