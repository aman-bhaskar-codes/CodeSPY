"""Complete-file output omissions, including actual report maps and excerpt overhead."""

from hashlib import sha256
from uuid import UUID

from app.analyzers.result_shape import draft_locations, location_key
from app.contracts.structure import (
    ArchitectureReportV1,
    FileOmissionV1,
    IdentityMapV1,
    SourceExcerptV1,
    SourceInputV1,
    SourceUnitV1,
    StructuralDraftV1,
)
from app.domain.scope import RunRef
from pydantic import ValidationError


def fits(draft: StructuralDraftV1, source: SourceInputV1, units: tuple[SourceUnitV1, ...]) -> bool:
    locations = draft_locations(draft)
    if len(locations) > source.config.limits.evidence:
        return False
    data = {u.file_id: u.data for u in units}
    excerpts, total = [], 0
    for loc in locations:
        raw = data[loc.file_id][loc.range.start_byte : loc.range.end_byte]
        if len(raw) <= 2048 and total + len(raw) <= 65536:
            excerpts.append(
                SourceExcerptV1(location=loc, text=raw.decode(), sha256=sha256(raw).hexdigest())
            )
            total += len(raw)
    # Physical UUIDs have constant JSON width; zero values are sizing placeholders only.
    # They never leave this function, appear in configuration, or become persisted IDs.
    zero = UUID(int=0)
    try:
        ArchitectureReportV1(
            **draft.model_dump(exclude={"excerpts"}),
            run_ref=RunRef(org_id=zero, snapshot_id=zero, run_id=zero),
            limits=source.config.limits,
            symbol_map=tuple(
                IdentityMapV1(semantic_key=s.semantic_key, physical_id=zero) for s in draft.symbols
            ),
            evidence_map=tuple(
                IdentityMapV1(semantic_key=location_key(loc), physical_id=zero) for loc in locations
            ),
            excerpts=tuple(excerpts),
        )
    except ValidationError:
        return False
    return True


def bound_draft(
    draft: StructuralDraftV1, source: SourceInputV1, units: tuple[SourceUnitV1, ...]
) -> StructuralDraftV1:
    while not fits(draft, source, units):
        # Large inventory omissions must not force a report beyond its byte cap.
        # Counters stay exact; inventory remains the complete per-file authority.
        if len(draft.coverage.omissions) > 128:
            kept = draft.coverage.omissions[: len(draft.coverage.omissions) // 2]
            coverage = draft.coverage.model_copy(
                update={
                    "omissions": kept,
                    "omitted_details_count": draft.coverage.omitted_files - len(kept),
                }
            )
            draft = draft.model_copy(update={"coverage": coverage})
            continue
        modules = sorted(
            (s for s in draft.symbols if s.kind == "module"), key=lambda s: s.path.encode()
        )
        if not modules:
            raise ValueError("report capacity cannot represent coverage")
        removed = modules[-1].file_id
        symbols = tuple(s for s in draft.symbols if s.file_id != removed)
        keys = {s.semantic_key for s in symbols}
        resolutions = []
        for resolution in draft.resolutions:
            if resolution.site_key not in keys:
                continue
            if not set(resolution.candidate_keys) <= keys:
                resolution = resolution.model_copy(
                    update={
                        "candidate_keys": (),
                        "status": "unresolved",
                        "reason": "output_capacity",
                    }
                )
            resolutions.append(resolution)
        routes = tuple(
            r
            for r in draft.routes
            if r.registration.file_id != removed
            and (r.handler is None or r.handler.file_id != removed)
            and all(loc.file_id != removed for loc in r.includes)
            and (r.handler_key is None or r.handler_key in keys)
        )
        requests = tuple(r for r in draft.requests if r.location.file_id != removed)
        route_keys, request_keys = {r.key for r in routes}, {r.key for r in requests}
        links = tuple(
            link
            for link in draft.links
            if link.request_key in request_keys and set(link.route_keys) <= route_keys
        )
        omissions = (
            *draft.coverage.omissions,
            FileOmissionV1(file_id=removed, path=modules[-1].path, reason="output_capacity"),
        )
        coverage = draft.coverage.model_copy(
            update={
                "processed_files": draft.coverage.processed_files - 1,
                "omitted_files": draft.coverage.omitted_files + 1,
                "omissions": omissions,
                "resolution_records": len(resolutions),
                "distinct_resolution_sites": len({r.site_key for r in resolutions}),
                "resolved": sum(r.status == "resolved" for r in resolutions),
                "ambiguous": sum(r.status == "ambiguous" for r in resolutions),
                "unresolved": sum(r.status == "unresolved" for r in resolutions),
                "syntax": "partial",
                "resolution": "partial",
                "framework": "partial",
                "graph": "partial",
            }
        )
        draft = draft.model_copy(
            update={
                "symbols": symbols,
                "resolutions": tuple(resolutions),
                "routes": routes,
                "requests": requests,
                "links": links,
                "coverage": coverage,
                "diagnostics": tuple(
                    d
                    for d in draft.diagnostics
                    if d.location is None or d.location.file_id != removed
                ),
                "components": tuple(
                    c.model_copy(
                        update={
                            "state": "unavailable",
                            "components": (),
                            "edges": (),
                            "reason": "output_capacity",
                        }
                    )
                    for c in draft.components
                ),
            }
        )
    return StructuralDraftV1.model_validate(draft.model_dump())
