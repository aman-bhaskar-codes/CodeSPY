"""What an agent tool returns to the model and to the event stream.

A tool never raises to the agent: a refusal, a bad argument or an unavailable service is a result
with a status and a safe message, so the model can adapt and the stream can show what happened.
"""

import json
from typing import Annotated, Any

from pydantic import Field, model_validator

from app.contracts.agent_events import EvidenceRef, ToolClass, ToolStatus
from app.domain.scope import Contract

MAX_RESULT_CONTENT_BYTES = 32 * 1024
MAX_RESULT_DATA_BYTES = 2 * 1024


class AgentToolArgs(Contract):
    """Base of every tool's arguments: strict types, no undeclared fields, no coercion."""


class AgentToolResultV1(Contract):
    status: ToolStatus
    summary: Annotated[str, Field(max_length=1024)] = ""
    #: Text shown to the model, already redacted and at most 32 KiB (head and tail kept).
    content: str = ""
    truncated: bool = False
    #: Size of the content before truncation, in bytes.
    bytes_total: int = Field(default=0, ge=0)
    #: How many secrets were replaced by markers in ``content``.
    redactions: int = Field(default=0, ge=0)
    #: Reference to the full output in the journal, when it was truncated.
    content_ref: Annotated[str, Field(max_length=128)] | None = None
    evidence: tuple[EvidenceRef, ...] = Field(default=(), max_length=50)
    #: A few structured facts for the runner (for example a file hash), not shown to the model.
    data: dict[str, Any] = Field(default_factory=dict)
    error_code: Annotated[str, Field(max_length=64, pattern=r"^[a-z0-9_]+$")] | None = None

    @model_validator(mode="after")
    def _bounded(self) -> "AgentToolResultV1":
        if len(self.content.encode()) > MAX_RESULT_CONTENT_BYTES:
            raise ValueError("tool content exceeds 32 KiB")
        if len(json.dumps(self.data, default=str)) > MAX_RESULT_DATA_BYTES:
            raise ValueError("tool data exceeds 2 KiB")
        if self.status in {"ok", "partial"} and self.error_code is not None:
            raise ValueError("a successful result carries no error code")
        return self


class AgentToolSpecV1(Contract):
    """How a tool is offered to the model."""

    name: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")]
    tool_class: ToolClass
    description: Annotated[str, Field(min_length=1, max_length=1024)]
    parameters_schema: dict[str, Any]
