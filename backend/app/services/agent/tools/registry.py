"""Looks up a tool, validates the model's arguments, and turns every failure into a result."""

import asyncio
import json
import logging
from collections.abc import Iterable

from pydantic import ValidationError

from app.contracts.agent_tools import AgentToolResultV1, AgentToolSpecV1
from app.contracts.model import ToolDefinitionV1
from app.ports.sandbox import (
    ExecRejected,
    NotATextFile,
    PathRejected,
    SandboxGone,
    SandboxLimit,
    SandboxUnavailable,
    WorkspaceFileNotFound,
)
from app.services.agent.tools.base import Tool, ToolContext, fail

logger = logging.getLogger(__name__)

MAX_ARGUMENT_BYTES = 16 * 1024
TOOL_SECONDS = 20


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool]) -> None:
        self._tools = {tool.name: tool for tool in tools}

    def specs(self) -> list[AgentToolSpecV1]:
        return [tool.spec() for tool in self._tools.values()]

    def model_definitions(self) -> list[ToolDefinitionV1]:
        """The tools in the form the model gateway sends to a provider."""
        return [
            ToolDefinitionV1(
                name=s.name, description=s.description, parameters_schema=s.parameters_schema
            )
            for s in self.specs()
        ]

    def tool(self, name: str) -> Tool | None:
        return self._tools.get(name)

    async def execute(self, ctx: ToolContext, name: str, arguments_json: str) -> AgentToolResultV1:
        """Run a tool for the model. Never raises: every outcome is a result."""
        tool = self._tools.get(name)
        if tool is None:
            return fail("invalid", "unknown_tool", f"There is no tool named {name[:64]!r}.")
        if len(arguments_json.encode()) > MAX_ARGUMENT_BYTES:
            return fail("invalid", "arguments_too_large", "The arguments are too large.")
        try:
            parsed = json.loads(arguments_json or "{}")
            if not isinstance(parsed, dict):
                raise ValueError("arguments must be an object")
            args = tool.args_model.model_validate_json(json.dumps(parsed))
        except ValidationError as error:
            fields = "; ".join(
                f"{'.'.join(map(str, e['loc'])) or 'arguments'}: {e['msg']}"
                for e in error.errors(include_input=False, include_url=False)[:5]
            )
            return fail("invalid", "invalid_arguments", f"Invalid arguments. {fields}")
        except ValueError:
            return fail("invalid", "invalid_arguments", "The arguments must be a JSON object.")
        try:
            async with asyncio.timeout(TOOL_SECONDS):
                return await tool.run(ctx, args)
        except TimeoutError:
            return fail("timed_out", "timed_out", f"{name} took too long and was stopped.")
        except PathRejected:
            return fail("denied", "path_rejected", "That path is not allowed in the workspace.")
        except WorkspaceFileNotFound:
            return fail("invalid", "file_not_found", "There is no such file in the workspace.")
        except NotATextFile:
            return fail("invalid", "not_text", "That path is not a readable text file.")
        except (SandboxLimit, ExecRejected):
            return fail("invalid", "limit_exceeded", "The request exceeds a sandbox limit.")
        except (SandboxGone, SandboxUnavailable):
            return fail("unavailable", "sandbox_unavailable", "The sandbox is not available.")
        except Exception:
            logger.exception("tool %s failed", name)  # the log has the cause; the model does not
            return fail("unavailable", "tool_failed", f"{name} failed unexpectedly.")
