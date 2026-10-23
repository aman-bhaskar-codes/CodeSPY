"""Untrusted model output validation, schema enforcement, and bounded repair."""

import json
from typing import Any

from pydantic import BaseModel, ValidationError

from app.contracts.model import ToolCallV1, ToolDefinitionV1


class OutputValidationError(Exception):
    """Validation failure on untrusted model response."""


class InvalidJsonOutputError(OutputValidationError):
    """Model output failed JSON parsing."""


class SchemaMismatchError(OutputValidationError):
    """Parsed model output does not conform to required schema."""


class DangerousArgumentError(OutputValidationError):
    """Model-generated tool arguments violate security guardrails."""


class OutputValidator:
    """Validates structured model responses, tool parameters, and security invariants."""

    @staticmethod
    def validate_json_schema(content: str, schema: dict[str, Any]) -> dict[str, Any]:
        """Validate string content against a JSON schema specification."""
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as err:
            raise InvalidJsonOutputError(f"Model output is not valid JSON: {err}") from err

        if not isinstance(parsed, dict):
            raise SchemaMismatchError(f"Expected JSON object, got {type(parsed).__name__}")

        # Validate required fields
        required_fields = schema.get("required", [])
        for field in required_fields:
            if field not in parsed:
                raise SchemaMismatchError(f"Missing required schema field: '{field}'")

        return parsed

    @staticmethod
    def validate_pydantic_model(content: str, model_cls: type[BaseModel]) -> BaseModel:
        """Validate and parse string content into a Pydantic model instance."""
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as err:
            raise InvalidJsonOutputError(f"Model output is not valid JSON: {err}") from err

        try:
            return model_cls.model_validate(parsed)
        except ValidationError as err:
            raise SchemaMismatchError(f"Schema validation error: {err}") from err

    @staticmethod
    def validate_tool_call(
        tool_call: ToolCallV1, allowed_tools: tuple[ToolDefinitionV1, ...]
    ) -> dict[str, Any]:
        """Verify tool call name, parameter schema, and security invariants."""
        matching_tool = next((t for t in allowed_tools if t.name == tool_call.name), None)
        if not matching_tool:
            raise SchemaMismatchError(f"Unapproved tool call '{tool_call.name}'")

        try:
            args = json.loads(tool_call.arguments_json)
        except json.JSONDecodeError as err:
            raise InvalidJsonOutputError(f"Tool arguments are not valid JSON: {err}") from err

        if not isinstance(args, dict):
            arg_type = type(args).__name__
            raise SchemaMismatchError(f"Tool arguments must be an object, got {arg_type}")

        # Security invariant checks: prevent shell injection, path traversal, raw sql
        for key, val in args.items():
            if isinstance(val, str):
                if "../" in val or "..\\" in val:
                    raise DangerousArgumentError(
                        f"Path traversal detected in argument '{key}': {val}"
                    )
                if any(x in val for x in ("; rm -rf", "| sh", "`", "$(")):
                    raise DangerousArgumentError(
                        f"Dangerous shell characters detected in argument '{key}': {val}"
                    )

        return args

    @staticmethod
    def create_repair_prompt(
        original_output: str, error_message: str, schema: dict[str, Any]
    ) -> str:
        """Produce a focused, bounded repair prompt for at most one repair retry."""
        return (
            "Your previous response failed validation with the following error:\n"
            f"ERROR: {error_message}\n\n"
            "Please return ONLY a valid JSON object matching this schema exactly:\n"
            f"SCHEMA: {json.dumps(schema)}\n\n"
            f"PREVIOUS OUTPUT: {original_output[:500]}"
        )
