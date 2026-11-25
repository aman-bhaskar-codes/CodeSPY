"""Pages of report statements as the narrative model is shown them.

One function builds the payload for the investigation tool and for the owner-run smoke script, so
what the model sees, what receipts hash and what the verifier later reads are the same bytes.
"""

import json
from dataclasses import dataclass
from typing import Any

from app.analyzers.graph.render import statement_text
from app.analyzers.graph.statement_ids import statement_ids
from app.contracts.architecture_report import RepositoryReportV1
from app.contracts.source_view import PrivacyPolicyV1
from app.domain.canonical_json import canonical_bytes
from app.services.privacy_transform import transform_model_safe

PAGE_BYTES = 16 * 1024
PAGE_STATEMENTS = 40


@dataclass(frozen=True)
class ReportPage:
    section: str
    total: int
    offset: int
    next_offset: int | None
    entries: tuple[dict[str, Any], ...]
    payload: str


def build_page(
    report: RepositoryReportV1,
    section: str,
    offset: int,
    policy: PrivacyPolicyV1,
    *,
    max_statements: int = PAGE_STATEMENTS,
    max_bytes: int = PAGE_BYTES,
) -> ReportPage | None:
    """The page of ``section`` starting at ``offset``, or None for an unknown section."""
    rows = statement_ids(report).get(section)
    if rows is None:
        return None
    entries: list[dict[str, Any]] = []
    size = 0
    index = offset
    while index < len(rows) and len(entries) < max_statements:
        sid, statement = rows[index]
        params = {
            key: (
                transform_model_safe(value.encode("utf-8"), policy).safe_text
                if isinstance(value, str)
                else value
            )
            for key, value in statement.params.items()
        }
        entry = {
            "sid": sid,
            "kind": statement.kind,
            "code": statement.code,
            "text": statement_text(statement.model_copy(update={"params": params})),
            "params": params,
        }
        cost = len(canonical_bytes(entry)) + 1
        if entries and size + cost > max_bytes:
            break
        entries.append(entry)
        size += cost
        index += 1
    next_offset = index if index < len(rows) else None
    payload = json.dumps(
        {
            "section": section,
            "total": len(rows),
            "offset": offset,
            "next_offset": next_offset,
            "statements": entries,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return ReportPage(section, len(rows), offset, next_offset, tuple(entries), payload)
