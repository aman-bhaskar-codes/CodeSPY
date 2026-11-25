"""A scripted agent model for tests: it answers with the turns it was given, in order.

It records every request it receives, so a test can assert what the loop showed the model (the
conversation so far, the tool results, the tools offered). It never reads the network.
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

from app.ports.agent_model import (
    Finished,
    ModelEvent,
    ModelRequest,
    ModelToolCall,
    ModelUnavailable,
    ModelUsage,
    TextDelta,
    ToolCallRequested,
)


@dataclass
class ScriptedTurn:
    text: str = ""
    tool_calls: list[tuple[str, str, str]] = field(
        default_factory=list
    )  # (id, name, arguments_json)
    usage: ModelUsage = field(default_factory=lambda: ModelUsage(100, 20, 0, 0, 1000))
    fail: ModelUnavailable | None = None
    #: Called with the request before the turn is produced (to assert on what the model was shown).
    inspect: Callable[[ModelRequest], None] | None = None
    delay_s: float = 0.0


class ScriptedAgentModel:
    def __init__(self, turns: list[ScriptedTurn]) -> None:
        self._turns = list(turns)
        self.requests: list[ModelRequest] = []

    @property
    def calls(self) -> int:
        return len(self.requests)

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        return self._stream(request)

    async def _stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        if not self._turns:
            raise AssertionError("the scripted model has no more turns")
        turn = self._turns.pop(0)
        if turn.inspect is not None:
            turn.inspect(request)
        if turn.delay_s:
            await asyncio.sleep(turn.delay_s)
        if turn.fail is not None:
            raise turn.fail
        for start in range(0, len(turn.text), 40):
            yield TextDelta(turn.text[start : start + 40])
        for call_id, name, arguments in turn.tool_calls:
            yield ToolCallRequested(ModelToolCall(call_id, name, arguments))
        yield Finished("tool_use" if turn.tool_calls else "end_turn", turn.usage)
