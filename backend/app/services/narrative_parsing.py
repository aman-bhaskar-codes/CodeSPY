"""Turning a model reply into a narrative conclusion, strictly and without silent fallbacks."""

import json
import re

from pydantic import ValidationError

from app.contracts.report_narrative import NarrativeConclusionV1

_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")
MAX_REPLY_CHARS = 64 * 1024


class NarrativeParseError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def extract_json_object(reply: str) -> str:
    """The JSON object in a reply: unfenced as-is, otherwise the first ``{`` to the last ``}``."""
    if len(reply) > MAX_REPLY_CHARS:
        raise NarrativeParseError("reply_too_large")
    text = _FENCE.sub("", reply.strip())
    if text.startswith("{") and text.endswith("}"):
        return text
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise NarrativeParseError("not_json")
    return text[start : end + 1]


def parse_conclusion(reply: str) -> NarrativeConclusionV1:
    body = extract_json_object(reply)
    try:
        json.loads(body)
    except ValueError:
        raise NarrativeParseError("not_json") from None
    try:
        return NarrativeConclusionV1.model_validate_json(body)
    except ValidationError:
        raise NarrativeParseError("schema_mismatch") from None
