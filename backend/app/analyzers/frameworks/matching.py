"""Match HTTP client request observations with candidate server route observations."""

from collections.abc import Sequence

from app.contracts.structure import (
    BoundaryLinkV1,
    HttpRequestObservationV1,
    RouteObservationV1,
    ServiceBindingV1,
)


def match_boundaries(
    routes: Sequence[RouteObservationV1],
    requests: Sequence[HttpRequestObservationV1],
    bindings: Sequence[ServiceBindingV1],
) -> tuple[BoundaryLinkV1, ...]:
    links: list[BoundaryLinkV1] = []

    canonical_origin_map = {b.canonical_origin.rstrip("/"): b for b in bindings}

    for req in requests:
        target_service: str | None = None

        if req.origin_state == "known" and req.base_url:
            matched_b = canonical_origin_map.get(req.base_url.rstrip("/"))
            if matched_b:
                target_service = matched_b.service_key
        elif req.origin_state == "relative":
            # Extract same_origin service from limitations
            for lim in req.limitations:
                if lim.startswith("same_origin:"):
                    target_service = lim.split(":", 1)[1]
                    break

        if not target_service:
            links.append(
                BoundaryLinkV1(
                    request_key=req.key,
                    route_keys=(),
                    state="unmatched",
                    reason="no_service_binding_or_route",
                    evidence_keys=(req.key,),
                    limitations=(),
                )
            )
            continue

        # Match routes belonging to target_service with same method and template_path
        candidates: list[RouteObservationV1] = []
        for r in routes:
            if (
                r.service_key == target_service
                and r.state == "resolved"
                and (r.method in ("ANY", req.method))
                and r.template_path == req.path
            ):
                candidates.append(r)

        if not candidates:
            links.append(
                BoundaryLinkV1(
                    request_key=req.key,
                    route_keys=(),
                    state="unmatched",
                    reason="no_matching_route_template",
                    evidence_keys=(req.key,),
                    limitations=(),
                )
            )
        elif len(candidates) == 1:
            c = candidates[0]
            links.append(
                BoundaryLinkV1(
                    request_key=req.key,
                    route_keys=(c.key,),
                    state="candidate",
                    reason="explicit_binding_match",
                    evidence_keys=(req.key, c.key),
                    limitations=(),
                )
            )
        else:
            links.append(
                BoundaryLinkV1(
                    request_key=req.key,
                    route_keys=tuple(c.key for c in candidates),
                    state="ambiguous",
                    reason="multiple_candidate_routes",
                    evidence_keys=(req.key, *tuple(c.key for c in candidates)),
                    limitations=(),
                )
            )

    return tuple(links)
