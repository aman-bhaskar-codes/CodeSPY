"""Shared pure identity/location projection for analyzer budgets and trusted publication."""

from app.contracts.structure import SourceLocationV1, StructuralDraftV1
from app.domain.canonical_json import semantic_digest


def location_key(location: SourceLocationV1) -> str:
    return semantic_digest(location.model_dump())


def draft_locations(draft: StructuralDraftV1) -> tuple[SourceLocationV1, ...]:
    locations = [s.location for s in draft.symbols]
    for route in draft.routes:
        locations.extend([route.registration, *route.includes])
        if route.handler:
            locations.append(route.handler)
    locations.extend(r.location for r in draft.requests)
    locations.extend(d.location for d in draft.diagnostics if d.location is not None)
    unique = {location_key(loc): loc for loc in locations}
    return tuple(unique[key] for key in sorted(unique))
