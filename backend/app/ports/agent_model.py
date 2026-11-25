"""The agent's view of a model: a conversation in, a stream of text and tool calls out.

This is the seam between the agent loop and any provider. The loop never sees a provider SDK, a
price list or a retry policy; an adapter turns a ``ModelRequest`` into whatever the provider
needs and its answer into ``ModelEvent`` values. The loop keeps what a call returned (as a
``ModelTurn``) in its journal, so a restarted run replays a model call instead of making it again.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal, Protocol

StopReason = Literal["end_turn", "tool_use", "max_tokens", "refusal", "error"]


@dataclass(frozen=True)
class ModelToolCall:
    id: str
    name: str
    arguments_json: str


@dataclass(frozen=True)
class ModelMessage:
    """One message of the conversation the model is shown."""

    role: Literal["user", "assistant", "tool"]
    text: str = ""
    tool_calls: tuple[ModelToolCall, ...] = ()
    #: For a ``tool`` message: the call it answers.
    tool_call_id: str | None = None


@dataclass(frozen=True)
class ModelToolSpec:
    name: str
    description: str
    parameters_schema: dict[str, object]


@dataclass(frozen=True)
class ModelRequest:
    system: str
    messages: tuple[ModelMessage, ...]
    tools: tuple[ModelToolSpec, ...]
    max_tokens: int = 8000
    #: The model profile the session asked for ("agent", "fast", "deep").
    profile: str = "agent"


@dataclass(frozen=True)
class ModelUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    #: What the call cost, in millionths of a dollar, as the adapter knows it.
    cost_micros: int = 0


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ToolCallRequested:
    call: ModelToolCall


@dataclass(frozen=True)
class Finished:
    stop_reason: StopReason
    usage: ModelUsage = field(default_factory=ModelUsage)
    model_id: str = "agent-model"


ModelEvent = TextDelta | ToolCallRequested | Finished


class AgentModelPort(Protocol):
    def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        """Events in order; the last is ``Finished``. May raise ``ModelUnavailable``."""
        ...


class ModelUnavailable(Exception):  # noqa: N818 -- public contract name
    """The model could not answer (transport, throttling, policy). The loop reports it and stops."""

    def __init__(self, code: str = "model_unavailable", retryable: bool = True) -> None:
        super().__init__(code)
        self.code, self.retryable = code, retryable
