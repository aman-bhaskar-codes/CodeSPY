"""Shared validation only; each model protocol retains its own wire mapping."""

import json
from typing import Any

from app.contracts.model import ModelRequestV1, ModelResponseV1, ToolCallV1
from app.contracts.model_configuration import ModelProfileV1
from app.contracts.model_transport import (
    PreparedModelCallV1,
    ProviderOutcomeV1,
    ProviderUsageEvidenceV1,
)
from app.domain.canonical_json import semantic_digest

_SCHEMA_KEYS = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "description",
        "anyOf",
        "title",
        "default",
    }
)


def validate_schema(schema: dict[str, Any], depth: int = 0) -> None:
    if depth > 8 or len(json.dumps(schema).encode()) > 65536:
        raise ValueError("schema exceeds supported bounds")
    if any(key not in _SCHEMA_KEYS for key in schema):
        raise ValueError("unsupported schema keyword")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict) or len(properties) > 128:
        raise ValueError("invalid schema properties")
    for child in properties.values():
        if not isinstance(child, dict):
            raise ValueError("invalid schema property")
        validate_schema(child, depth + 1)
    if "items" in schema:
        if not isinstance(schema["items"], dict):
            raise ValueError("invalid items schema")
        validate_schema(schema["items"], depth + 1)
    for child in schema.get("anyOf", []):
        if not isinstance(child, dict):
            raise ValueError("invalid alternative schema")
        validate_schema(child, depth + 1)


def validate_request(request: ModelRequestV1, profile: ModelProfileV1) -> None:
    if not profile.enabled or not profile.locally_qualified:
        raise ValueError("profile unavailable")
    if request.provider != profile.provider.value or request.model_id != profile.model_id:
        raise ValueError("request does not match profile")
    if request.max_tokens > profile.max_output_tokens:
        raise ValueError("output limit exceeds profile")
    if request.tools and not profile.supports_tools:
        raise ValueError("tools unsupported")
    if len(request.tools) > 16:
        raise ValueError("too many tool definitions")
    names = [tool.name for tool in request.tools]
    if len(names) != len(set(names)):
        raise ValueError("duplicate tool definition")
    for tool in request.tools:
        validate_schema(tool.parameters_schema)
    if request.tool_choice not in ("auto", "none", "required", *names):
        raise ValueError("unknown tool choice")
    if request.response_schema is not None:
        if not profile.supports_native_schema:
            raise ValueError("native schema unsupported")
        validate_schema(request.response_schema)
    if profile.provider.value == "openai" and profile.reasoning_mode == "bounded":
        raise ValueError("numeric thinking budgets are unsupported by Responses")


def prepared(
    request: ModelRequestV1, profile: ModelProfileV1, body: dict[str, Any]
) -> PreparedModelCallV1:
    serialized = json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    if len(serialized.encode()) > profile.request_byte_limit:
        raise ValueError("request exceeds profile byte limit")
    return PreparedModelCallV1(
        profile=profile,
        request=request,
        body_json=serialized,
        wire_digest=semantic_digest({"body": body, "profile": profile.model_dump(mode="json")}),
    )


def tool_call(identifier: Any, name: Any, arguments: Any, profile: ModelProfileV1) -> ToolCallV1:
    if isinstance(arguments, dict):
        arguments = json.dumps(arguments, separators=(",", ":"), allow_nan=False)
    if not isinstance(arguments, str) or len(arguments.encode()) > profile.tool_argument_byte_limit:
        raise ValueError("tool arguments exceed supported bounds")
    if not isinstance(json.loads(arguments), dict):
        raise ValueError("tool arguments must be an object")
    # Long native IDs require a durable bidirectional continuation mapping, never truncation.
    if not isinstance(identifier, str) or not isinstance(name, str):
        raise ValueError("invalid tool identity")
    return ToolCallV1(id=identifier, name=name, arguments_json=arguments)


def outcome(
    request: ModelRequestV1,
    profile: ModelProfileV1,
    usage: ProviderUsageEvidenceV1,
    text: str,
    calls: list[ToolCallV1],
    finish: str,
    request_id: Any = None,
) -> ProviderOutcomeV1:
    if usage.usage is None:
        return ProviderOutcomeV1(usage=usage, failure_code="missing_usage")
    if len(calls) > profile.tool_call_limit:
        return ProviderOutcomeV1(usage=usage, failure_code="invalid_output")
    if request_id is not None and (not isinstance(request_id, str) or len(request_id) > 128):
        return ProviderOutcomeV1(usage=usage, failure_code="unsupported_protocol")
    response = ModelResponseV1.model_validate(
        {
            "call_id": request.call_id,
            "attempt_id": request.attempt_id,
            "provider": request.provider,
            "model_id": request.model_id,
            "content": text,
            "tool_calls": tuple(calls),
            "usage": usage.usage,
            "charge_micros": 0,
            "finish_reason": finish,
            "provider_request_id": request_id,
        }
    )
    return ProviderOutcomeV1(response=response, usage=usage)


def internal_tool_id(native_id: Any, request: ModelRequestV1, index: int) -> str:
    if isinstance(native_id, str) and 0 < len(native_id) <= 64:
        return native_id
    if native_id is not None and not isinstance(native_id, str):
        raise ValueError("invalid native tool identity")
    return "tool_" + semantic_digest([str(request.call_id), index, native_id])[:48]


def native_history(request: ModelRequestV1, reference: Any) -> list[dict[str, Any]]:
    value = json.loads(request.opaque_context_json).get(str(reference))
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError("missing scoped protocol continuation")
    return value


def with_continuation(result: ProviderOutcomeV1, blocks: list[dict[str, Any]]) -> ProviderOutcomeV1:
    if result.response is None:
        return result
    encoded = json.dumps(blocks, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    if len(encoded.encode()) > 262144:
        return ProviderOutcomeV1(usage=result.usage, failure_code="unsupported_protocol")
    return ProviderOutcomeV1(
        response=result.response,
        usage=result.usage,
        failure_code=result.failure_code,
        continuation_json=encoded,
    )


def validate_output_schema(value: Any, schema: dict[str, Any]) -> None:
    """Enforce the admitted bounded JSON-schema subset without remote references."""
    from app.services.output_validator import SchemaMismatchError

    def mismatch() -> None:
        raise SchemaMismatchError("model output violates the admitted schema")

    if "anyOf" in schema:
        for alternative in schema["anyOf"]:
            try:
                validate_output_schema(value, alternative)
                break
            except SchemaMismatchError:
                continue
        else:
            mismatch()
    expected = schema.get("type")
    checks = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": type(value) is int,
        "number": type(value) in (int, float),
        "boolean": type(value) is bool,
        "null": value is None,
    }
    if expected is not None and not any(
        checks.get(kind, False) for kind in (expected if isinstance(expected, list) else [expected])
    ):
        mismatch()
    if "enum" in schema and not any(
        type(value) is type(item) and value == item for item in schema["enum"]
    ):
        mismatch()
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if any(key not in value for key in schema.get("required", [])):
            mismatch()
        if schema.get("additionalProperties") is False and any(
            key not in properties for key in value
        ):
            mismatch()
        for key, item in value.items():
            if key in properties:
                validate_output_schema(item, properties[key])
    if isinstance(value, list) and "items" in schema:
        for item in value:
            validate_output_schema(item, schema["items"])
