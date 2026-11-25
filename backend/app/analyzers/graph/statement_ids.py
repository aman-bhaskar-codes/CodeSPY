"""Stable ids for report statements, so a narrative can cite exactly what it was shown.

A statement has no id in the report itself (its ``code`` repeats within a section), so the id is
computed: a hash of the section, code and parameters, with an occurrence index when several
statements of one section are identical. It is only meaningful together with the report digest.
"""

import hashlib
from collections import Counter

from app.contracts.architecture_report import RepositoryReportV1, StatementV1
from app.domain.canonical_json import canonical_bytes

SID_LENGTH = 16


def _base(section: str, statement: StatementV1) -> str:
    material = {"section": section, "code": statement.code, "params": statement.params}
    return hashlib.sha256(canonical_bytes(material)).hexdigest()[:SID_LENGTH]


def statement_ids(report: RepositoryReportV1) -> dict[str, list[tuple[str, StatementV1]]]:
    """Per section name, its statements in report order with their ids."""
    result: dict[str, list[tuple[str, StatementV1]]] = {}
    for section in report.sections:
        seen: Counter[str] = Counter()
        rows: list[tuple[str, StatementV1]] = []
        for statement in section.statements:
            base = _base(section.name, statement)
            occurrence = seen[base]
            seen[base] += 1
            sid = (
                base
                if occurrence == 0
                else hashlib.sha256(f"{base}:{occurrence}".encode()).hexdigest()[:SID_LENGTH]
            )
            rows.append((sid, statement))
        result[section.name] = rows
    return result


def find_statement(report: RepositoryReportV1, sid: str) -> tuple[str, StatementV1] | None:
    """The (section, statement) a sid names in this report, or None."""
    for name, rows in statement_ids(report).items():
        for known, statement in rows:
            if known == sid:
                return name, statement
    return None
