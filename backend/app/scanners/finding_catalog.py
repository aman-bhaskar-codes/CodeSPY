"""Reviewed claim wording for the qualified offline predicates."""

from typing import Literal, Self

from pydantic import Field, model_validator

from app.contracts.findings import Category, RecommendationV1
from app.contracts.scanners import Digest, RuleId, ScannerProfileV1
from app.domain.canonical_json import semantic_digest
from app.domain.scope import Contract


class FindingPolicyV1(Contract):
    rule_id: RuleId
    category: Category
    profile_digest: Digest
    provider_version: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=256)
    severity: Literal["info", "low", "medium", "high", "critical"]
    severity_basis: str = Field(min_length=1, max_length=512)
    recommendation: RecommendationV1
    limitations: tuple[str, ...] = Field(min_length=1, max_length=8)


class FindingCatalogV1(Contract):
    policy_id: RuleId = "offline-predicates"
    policy_version: RuleId = "1"
    native: tuple[FindingPolicyV1, ...] = Field(max_length=3)

    @model_validator(mode="after")
    def unique(self) -> Self:
        if len({(r.profile_digest, r.rule_id) for r in self.native}) != len(self.native):
            raise ValueError("duplicate finding policy")
        return self

    def semantic_hash(self) -> str:
        return semantic_digest(self.model_dump())


def reviewed_catalog(profiles: tuple[ScannerProfileV1, ...]) -> FindingCatalogV1:
    """Bind reviewed wording to profiles supplied by trusted runtime composition."""
    policies = []
    for profile in profiles:
        for rule in profile.rule_ids:
            category: Category = "source_pattern"
            severity: Literal["info", "medium"] = "medium"
            if profile.tool == "semgrep" and rule == "python.shell-true":
                title, action = (
                    "Explicit shell execution",
                    "Prefer an argument list with shell disabled.",
                )
                benefit, trade_off = (
                    "Reduce shell interpretation.",
                    "Shell syntax needs explicit replacement.",
                )
                limitation = "Static pattern; command injection and runtime execution are unproven."
            elif profile.tool == "semgrep" and rule == "python.requests-verify-false":
                title, action = (
                    "Certificate verification disabled",
                    "Enable certificate verification with a trusted CA.",
                )
                benefit, trade_off = (
                    "Authenticate the remote certificate.",
                    "Private CAs need explicit configuration.",
                )
                limitation = (
                    "Static pattern; runtime configuration and exploitability are unproven."
                )
            elif profile.tool == "gitleaks" and rule == "secret.github-classic-pat":
                category, severity = "secret_format", "info"
                title, action = (
                    "Classic GitHub token-like format",
                    "Review the location and rotate the credential if genuine.",
                )
                benefit, trade_off = (
                    "Address a possible credential exposure.",
                    "Rotation may require updating consumers.",
                )
                limitation = (
                    "Selected format only; active credential validity and access are unverified."
                )
            else:
                raise ValueError("rule has no reviewed finding policy")
            policies.append(
                FindingPolicyV1(
                    rule_id=rule,
                    category=category,
                    profile_digest=semantic_digest(profile.model_dump()),
                    provider_version="1.179.0" if profile.tool == "semgrep" else "8.30.1",
                    title=title,
                    severity=severity,
                    severity_basis=(
                        "Reviewed static review priority; no exploitability or CVSS inference."
                    ),
                    recommendation=RecommendationV1(
                        action=action,
                        benefit=benefit,
                        trade_off=trade_off,
                        effort="unknown",
                        source=rule,
                    ),
                    limitations=(limitation,),
                )
            )
    return FindingCatalogV1(
        native=tuple(sorted(policies, key=lambda p: (p.profile_digest, p.rule_id)))
    )
