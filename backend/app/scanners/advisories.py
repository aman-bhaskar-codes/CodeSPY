"""Offline exact-query evidence, with freshness and completeness separate from matches.

Bundles are maintainer-supplied, versioned inputs, never uploaded target content.
No package identity leaves this process and no range/version resolver is invoked.
"""

from datetime import datetime, timedelta
from typing import Any, Literal, Self

from pydantic import Field, model_validator

from app.contracts.dependencies import AdvisoryAssessment as AdvisoryAssessment
from app.contracts.dependencies import PackageQuery as PackageQuery
from app.domain.canonical_json import canonical_bytes, semantic_digest
from app.domain.scope import Contract


class ExactAnswer(Contract):
    query: PackageQuery
    fetched_at: datetime
    complete: bool
    advisory_ids: tuple[str, ...] = Field(max_length=100)

    @model_validator(mode="after")
    def valid(self) -> Self:
        if self.fetched_at.utcoffset() is None or len(set(self.advisory_ids)) != len(
            self.advisory_ids
        ):
            raise ValueError("invalid exact query evidence")
        if any(not 1 <= len(identity) <= 128 for identity in self.advisory_ids):
            raise ValueError("invalid advisory identity")
        return self


class AdvisoryRecord(Contract):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    modified: datetime
    source: str = Field(max_length=512)
    document: dict[str, Any]

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if self.modified.utcoffset() is None or len(canonical_bytes(self.document)) > 262144:
            raise ValueError("invalid advisory record")
        if self.source != "https://api.osv.dev/v1/vulns/" + self.id:
            raise ValueError("invalid advisory source")
        return self

    def digest(self) -> str:
        return semantic_digest(self.model_dump())


class AdvisoryBundle(Contract):
    schema_version: Literal[1] = 1
    answers: tuple[ExactAnswer, ...] = Field(max_length=5000)
    records: tuple[AdvisoryRecord, ...] = Field(max_length=1000)

    @model_validator(mode="after")
    def unique_and_bounded(self) -> Self:
        if (
            len({answer.query.key() for answer in self.answers}) != len(self.answers)
            or len({record.id for record in self.records}) != len(self.records)
            or len(canonical_bytes(self.model_dump())) > 8388608
        ):
            raise ValueError("invalid advisory bundle")
        return self

    def digest(self) -> str:
        return semantic_digest(self.model_dump())

    def evaluate(self, query: PackageQuery, *, now: datetime) -> AdvisoryAssessment:
        """A complete fresh exact answer supports membership, never reachability."""
        if now.utcoffset() is None:
            raise ValueError("aware clock required")
        answer = next((a for a in self.answers if a.query.key() == query.key()), None)
        if answer is None:
            return AdvisoryAssessment(status="missing")
        if not timedelta(0) <= now - answer.fetched_at <= timedelta(days=7):
            return AdvisoryAssessment(status="stale")
        if not answer.complete:
            return AdvisoryAssessment(status="incomplete")
        records = {record.id: record for record in self.records}
        accepted = []
        for identity in answer.advisory_ids:
            record = records.get(identity)
            if record is None:
                return AdvisoryAssessment(status="incomplete")
            document = record.document
            try:
                modified = datetime.fromisoformat(document["modified"].replace("Z", "+00:00"))
                if (
                    document["id"] != record.id
                    or modified != record.modified
                    or record.modified > answer.fetched_at
                ):
                    return AdvisoryAssessment(status="invalid")
                if document.get("withdrawn") is not None:
                    return AdvisoryAssessment(status="withdrawn")
                affected = document["affected"]
                if not isinstance(affected, list) or not any(
                    item.get("package", {}).get("ecosystem") == query.ecosystem
                    and item.get("package", {}).get("name") == query.name
                    for item in affected
                ):
                    return AdvisoryAssessment(status="invalid")
            except (ValueError, KeyError, TypeError, AttributeError):
                return AdvisoryAssessment(status="invalid")
            accepted.append((identity, record.digest()))
        return AdvisoryAssessment(
            status="matched" if accepted else "empty", advisories=tuple(accepted)
        )
