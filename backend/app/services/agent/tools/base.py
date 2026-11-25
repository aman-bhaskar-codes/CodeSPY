"""The shape of a tool, and how every tool turns an outcome into an ``AgentToolResultV1``."""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, ClassVar

from app.contracts.agent_events import EvidenceRef, ToolClass, ToolStatus
from app.contracts.agent_tools import (
    MAX_RESULT_CONTENT_BYTES,
    AgentToolArgs,
    AgentToolResultV1,
    AgentToolSpecV1,
)
from app.contracts.workspace import SandboxHandleV1
from app.ports.sandbox import SandboxPort
from app.services.agent.tools.ports import BaseFilesPort, CodeSearchPort, SymbolIndexPort
from app.services.agent.tools.text import redact, truncate_head_tail


@dataclass(frozen=True)
class ToolContext:
    """What a tool may touch: one sandbox, and the optional services over the base snapshot."""

    sandbox: SandboxPort
    handle: SandboxHandleV1
    base: BaseFilesPort | None = None
    symbols: SymbolIndexPort | None = None
    search: CodeSearchPort | None = None


def succeed(
    content: str,
    *,
    summary: str,
    partial: bool = False,
    evidence: Iterable[EvidenceRef] = (),
    data: dict[str, Any] | None = None,
    already_redacted: int = 0,
    redact_content: bool = True,
) -> AgentToolResultV1:
    """A result with its content redacted and bounded. Redaction runs before truncation."""
    text, count = redact(content) if redact_content else (content, 0)
    cut = truncate_head_tail(text, MAX_RESULT_CONTENT_BYTES)
    return AgentToolResultV1(
        status="partial" if partial or cut.truncated else "ok",
        summary=summary[:1024],
        content=cut.text,
        truncated=cut.truncated,
        bytes_total=cut.bytes_total,
        redactions=count + already_redacted,
        evidence=tuple(evidence)[:50],
        data=data or {},
    )


def fail(status: ToolStatus, code: str, message: str) -> AgentToolResultV1:
    """A refusal or failure with a short, safe message. Never carries file content."""
    return AgentToolResultV1(
        status=status,
        summary=message[:1024],
        content=message[:2048],
        bytes_total=len(message.encode()),
        error_code=code,
    )


class Tool:
    """One capability offered to the model. Subclasses set the class attributes and ``run``."""

    name: ClassVar[str]
    description: ClassVar[str]
    args_model: ClassVar[type[AgentToolArgs]]
    tool_class: ClassVar[ToolClass] = "read"

    def spec(self) -> AgentToolSpecV1:
        schema = self.args_model.model_json_schema()
        schema.pop("title", None)
        return AgentToolSpecV1(
            name=self.name,
            tool_class=self.tool_class,
            description=self.description,
            parameters_schema=schema,
        )

    async def run(self, ctx: ToolContext, args: Any) -> AgentToolResultV1:
        raise NotImplementedError
