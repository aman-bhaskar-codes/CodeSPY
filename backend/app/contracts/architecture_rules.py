"""Declared architecture rules checked against a stored report's dependency graph.

Rules are evaluated on demand and never stored, so they cannot change a report's identity. They
follow the contract kinds of Import Linter: ``forbidden`` (the first group may not import the
second), ``layers`` (groups run from the top layer to the bottom one; a lower layer may not import
a higher one) and ``independence`` (no group may import another). A group is a set of path
prefixes. Only resolved imports are evidence of a dependency; ambiguous and unresolved imports from
a group are reported as unknown, never as passed.
"""

from typing import Literal, Self

from pydantic import Field, model_validator

from app.contracts.architecture_report import EvidenceRefV1
from app.contracts.structure import normalize_prefixes
from app.domain.scope import Contract

RuleKind = Literal["forbidden", "layers", "independence"]
RuleStatus = Literal["passed", "violated", "unknown"]
MAX_RULES = 16
MAX_GROUPS = 8
MAX_PREFIXES = 8
MAX_VIOLATIONS = 50


class RuleV1(Contract):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9 _.-]*$")
    kind: RuleKind
    groups: tuple[tuple[str, ...], ...] = Field(min_length=2, max_length=MAX_GROUPS)
    allow_indirect: bool = False  # forbidden and independence only: ignore chains of imports

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.kind == "forbidden" and len(self.groups) != 2:
            raise ValueError("a forbidden rule has exactly two groups: importer and imported")
        normalized = []
        for group in self.groups:
            if not 1 <= len(group) <= MAX_PREFIXES:
                raise ValueError("each group needs one to eight path prefixes")
            clean = normalize_prefixes(group)
            if len(clean) != len(group):
                raise ValueError("path prefixes must be distinct and well formed")
            normalized.append(clean)
        flat = [p for g in normalized for p in g]
        if len(set(flat)) != len(flat):
            raise ValueError("a prefix may belong to one group only")
        object.__setattr__(self, "groups", tuple(normalized))
        return self


class RuleSetV1(Contract):
    schema_version: Literal[1] = 1
    rules: tuple[RuleV1, ...] = Field(min_length=1, max_length=MAX_RULES)

    @model_validator(mode="after")
    def unique_names(self) -> Self:
        if len({r.name for r in self.rules}) != len(self.rules):
            raise ValueError("rule names must be unique")
        return self


class ViolationV1(Contract):
    source: str = Field(max_length=1024)
    target: str = Field(max_length=1024)
    chain: tuple[str, ...] = Field(max_length=12)  # source, intermediate files, target
    evidence: EvidenceRefV1 | None = None  # the first import of the chain


class RuleResultV1(Contract):
    name: str
    kind: RuleKind
    status: RuleStatus
    violations_total: int = Field(ge=0)
    violations: tuple[ViolationV1, ...] = Field(default=(), max_length=MAX_VIOLATIONS)
    unknown_imports: int = Field(default=0, ge=0)  # unlocated imports out of the rule's groups
    files_in_scope: int = Field(default=0, ge=0)


class RuleCheckV1(Contract):
    schema_version: Literal[1] = 1
    report_id: str
    inputs_digest: str
    results: tuple[RuleResultV1, ...]
