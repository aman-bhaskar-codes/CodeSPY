"""Map compiler byte positions to admitted syntax identities without guessing targets."""

from app.adapters.typescript import TypeScriptAdapter
from app.analyzers.positions import SourceIndex
from app.contracts.structure import (
    AnalysisBudget,
    AnalysisConfigV1,
    DiagnosticV1,
    ResolutionV1,
    SourceUnitV1,
    SymbolObservationV1,
)


def resolve_typescript(
    units: tuple[SourceUnitV1, ...],
    symbols: list[SymbolObservationV1],
    configs: list[dict[str, str]],
    config: AnalysisConfigV1,
    budget: AnalysisBudget,
) -> tuple[list[ResolutionV1], list[DiagnosticV1], bool]:
    selected = {u.path: u for u in units if u.language != "python"}
    if not selected:
        return [], [], False
    if config.resolution_profile == "syntax_only":
        return (
            [],
            [
                DiagnosticV1(
                    code="resolution_unavailable",
                    stage="resolution",
                    message="Compiler resolution was not requested.",
                )
            ],
            True,
        )
    result = TypeScriptAdapter().resolve(
        {
            "schema_version": 1,
            "files": [{"path": p, "text": u.data.decode()} for p, u in sorted(selected.items())],
            "configs": configs,
            "max_sites": config.limits.resolution_sites,
        },
        budget,
    )
    indexes = {p: SourceIndex(u.data) for p, u in selected.items()}
    records: dict[tuple[str, str, str], ResolutionV1] = {}
    for row in result["resolutions"]:
        budget.tick_nodes(0)
        index = indexes[row["path"]]
        start, end = (index.utf16_to_byte(row[k]) for k in ("start", "end"))
        sites = [
            s
            for s in symbols
            if s.path == row["path"]
            and s.kind == ("import" if row["resolution_kind"] == "import" else "call")
            and s.location.range.start_byte <= start <= end <= s.location.range.end_byte
        ]
        if not sites:
            continue
        site = min(sites, key=lambda s: s.location.range.end_byte - s.location.range.start_byte)
        # Identifiers deeper in arguments belong to their own syntax sites, not the outer call.
        if row["resolution_kind"] == "binding" and start != site.location.range.start_byte:
            continue
        candidates = []
        missing = False
        for candidate in row["candidates"]:
            ci = indexes.get(candidate["path"])
            if ci is None:
                missing = True
                continue
            a, b = (ci.utf16_to_byte(candidate[k]) for k in ("start", "end"))
            matches = [
                s
                for s in symbols
                if s.path == candidate["path"]
                and s.role == "declaration"
                and s.name == candidate["name"]
                and s.location.range.start_byte <= a <= b <= s.location.range.end_byte
            ]
            if not matches:
                missing = True
                continue
            target = min(
                matches, key=lambda s: s.location.range.end_byte - s.location.range.start_byte
            )
            candidates.append(target.semantic_key)
        keys = tuple(sorted(set(candidates)))
        reason = row["reason"]
        if missing or len(keys) > config.limits.candidates_per_site:
            keys = ()
            reason = "declaration_outside_graph" if missing else "candidate_budget"
        record = ResolutionV1(
            site_key=site.semantic_key,
            namespace=row["namespace"],
            resolution_kind=row["resolution_kind"],
            candidate_keys=keys,
            status="ambiguous" if len(keys) > 1 else "resolved" if keys else "unresolved",
            reason=reason[:128],
            basis="compiler_symbol",
        )
        key = (record.site_key, record.namespace, record.resolution_kind)
        if key in records and records[key] != record:
            record = record.model_copy(
                update={
                    "candidate_keys": (),
                    "status": "unresolved",
                    "reason": "multiple_site_bindings",
                }
            )
        records[key] = record
    diagnostics = [
        DiagnosticV1(
            code="config_out_of_scope",
            stage="resolution",
            message="Compiler project context is incomplete.",
        )
        for _ in result["diagnostics"]
    ]
    return (
        [records[k] for k in sorted(records)],
        diagnostics,
        result["truncated"] or bool(diagnostics),
    )
