"""What the agent may do without asking: the default policy.

The effective policy of a session will be the organisation's policy narrowed by the session's own
settings, and deny will win. This is the default every session starts from: reads run on their own,
every patch and every command waits for a person, publishing and network tools are not offered. A
person can approve a command once, or for the rest of the session with a rule (``exec: "pytest*"``).
Risk is a signal on the approval card; the decision comes from the class, the mode and the rules.
"""

import fnmatch
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

Verdict = Literal["allow", "ask", "deny"]


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: str = ""
    #: The rule that decided it, shown on the approval card.
    rule: str | None = None


@dataclass(frozen=True)
class PolicyV1:
    #: An organisation may apply edits on its own: checkpoints make them reversible.
    auto_apply_edits: bool = False
    #: Patterns the person approved for this session, such as ``pytest*``.
    session_rules: tuple[str, ...] = ()

    def with_rules(self, rules: Iterable[str]) -> "PolicyV1":
        return PolicyV1(self.auto_apply_edits, tuple(dict.fromkeys([*self.session_rules, *rules])))

    def decide(self, tool_class: str, rule_key: str, *, mode: str) -> Decision:
        if tool_class == "read":
            return Decision("allow", "reading changes nothing")
        if mode != "act":
            return Decision("deny", "this session is in plan mode and may only read")
        if tool_class == "edit":
            if self.auto_apply_edits:
                return Decision(
                    "allow", "the organisation applies edits automatically", "edit: auto"
                )
            return Decision("ask", "an edit needs approval")
        if tool_class == "exec":
            for pattern in self.session_rules:
                if fnmatch.fnmatchcase(rule_key, pattern):
                    return Decision("allow", "approved earlier in this session", f"exec: {pattern}")
            return Decision("ask", "a command needs approval")
        return Decision("deny", f"{tool_class} tools are not available")
