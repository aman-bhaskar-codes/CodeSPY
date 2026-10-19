"""Budgeted structural analysis pipeline producing deterministic StructuralDraftV1."""

from pathlib import Path

from app.adapters.typescript import TypeScriptProtocolError
from app.analyzers.frameworks.express import observe_express
from app.analyzers.frameworks.fastapi import observe_fastapi
from app.analyzers.frameworks.http_client import observe_http
from app.analyzers.frameworks.matching import match_boundaries
from app.analyzers.languages import extract_syntax
from app.analyzers.source import DIALECTS, AdmittedSourceReader
from app.analyzers.symbols.python import resolve_python
from app.analyzers.symbols.typescript import resolve_typescript
from app.contracts.structure import (
    AnalysisBudget,
    ComponentSectionV1,
    DiagnosticV1,
    FileOmissionV1,
    HttpRequestObservationV1,
    ResolutionV1,
    RouteObservationV1,
    SourceInputV1,
    StructuralCoverageV1,
    StructuralDraftV1,
    SymbolObservationV1,
)
from app.core.dsa.condensation import condense
from app.core.dsa.projection import build_projection
from app.core.dsa.puretypes import GraphEdge


def analyze(
    source_input: SourceInputV1,
    root: Path,
    budget: AnalysisBudget | None = None,
    *,
    isolated_operations: bool = False,
) -> StructuralDraftV1:
    actual_budget = budget or AnalysisBudget(
        wall_seconds=float(source_input.config.limits.analysis_seconds),
        max_syntax_nodes=source_input.config.limits.syntax_nodes,
    )

    # 1. Read admitted sources
    reader = AdmittedSourceReader(source_input, root)
    units = reader.read_selected()
    sources_map = {u.path: u for u in units}
    files_by_path = {f.path: f for f in source_input.files}

    # 2. Syntax extraction
    all_symbols: list[SymbolObservationV1] = []
    all_diagnostics: list[DiagnosticV1] = []
    files_obs = []

    for unit in units:
        actual_budget.tick_nodes(0)
        if isolated_operations:
            from app.analyzers.syntax_process import extract_isolated

            file_obs = extract_isolated(
                unit, actual_budget, source_input.config.limits.operation_seconds
            )
        else:
            file_obs = extract_syntax(unit, actual_budget)
        limit = source_input.config.limits
        # Reserve one evidence identity for each observation and framework/diagnostic locations.
        if len(all_symbols) + len(file_obs.symbols) > min(
            limit.nodes, limit.evidence // 2, limit.edges // 2
        ):
            reader.omitted.append((unit.path, "output_capacity"))
            continue
        files_obs.append(file_obs)
        all_symbols.extend(file_obs.symbols)
        all_diagnostics.extend(file_obs.diagnostics)

    included = {f.file_id for f in files_obs}
    units = tuple(u for u in units if u.file_id in included)
    sources_map = {u.path: u for u in units}
    # 3. Python resolution
    py_resolutions, py_diags = resolve_python(
        files_obs, sources_map, source_input.config, actual_budget
    )
    all_diagnostics.extend(py_diags)
    resolutions: list[ResolutionV1] = list(py_resolutions)

    ts_incomplete = False
    if "js_ts" in source_input.config.adapters:
        try:
            ts_resolutions, ts_diags, ts_incomplete = resolve_typescript(
                units, all_symbols, reader.configs, source_input.config, actual_budget
            )
            resolutions.extend(ts_resolutions)
            all_diagnostics.extend(ts_diags)
        except TypeScriptProtocolError:
            # Standard profile must not silently qualify without its compiler.
            raise

    # 4. Framework observations
    symbols_tuple = tuple(all_symbols)
    routes: list[RouteObservationV1] = []
    if "fastapi" in source_input.config.adapters:
        fastapi_routes, fastapi_diags = observe_fastapi(units, symbols_tuple, source_input.config)
        routes.extend(fastapi_routes)
        all_diagnostics.extend(fastapi_diags)

    if "express" in source_input.config.adapters:
        express_routes, express_diags = observe_express(units, symbols_tuple, source_input.config)
        routes.extend(express_routes)
        all_diagnostics.extend(express_diags)

    http_requests: list[HttpRequestObservationV1] = []
    if "http_client" in source_input.config.adapters:
        http_requests_obs, http_diags = observe_http(units, symbols_tuple, source_input.config)
        http_requests.extend(http_requests_obs)
        all_diagnostics.extend(http_diags)

    links = match_boundaries(routes, http_requests, source_input.config.service_bindings)

    limits = source_input.config.limits
    resolution_truncated = len(resolutions) > limits.resolution_sites
    resolutions = resolutions[: limits.resolution_sites]
    kept = []
    edge_count = sum(s.container_key is not None for s in all_symbols)
    for resolution in resolutions:
        cost = (
            len(resolution.candidate_keys)
            if resolution.resolution_kind in {"import", "call_target"}
            else 0
        )
        if edge_count + cost > limits.edges:
            resolution = resolution.model_copy(
                update={"candidate_keys": (), "status": "unresolved", "reason": "output_capacity"}
            )
            resolution_truncated = True
        else:
            edge_count += cost
        kept.append(resolution)
    resolutions = kept
    framework_truncated = len(routes) > 256 or len(http_requests) > 256 or len(links) > 512
    routes, http_requests = routes[:256], http_requests[:256]
    route_keys, request_keys = {r.key for r in routes}, {r.key for r in http_requests}
    links = tuple(
        link
        for link in links
        if link.request_key in request_keys and set(link.route_keys) <= route_keys
    )[:512]

    # 5. SCC graph projections (Imports)
    module_nodes = [s.semantic_key for s in all_symbols if s.kind == "module"]
    import_edges = []
    for r in resolutions:
        if r.resolution_kind == "import" and r.status == "resolved" and r.candidate_keys:
            # Find containing module for site_key
            target_key = r.candidate_keys[0]
            site_sym = next((s for s in all_symbols if s.semantic_key == r.site_key), None)
            if site_sym:
                source_mod = next(
                    (s for s in all_symbols if s.path == site_sym.path and s.kind == "module"),
                    None,
                )
                target_sym = next((s for s in all_symbols if s.semantic_key == target_key), None)
                target_mod = (
                    next(
                        (
                            s
                            for s in all_symbols
                            if s.path == target_sym.path and s.kind == "module"
                        ),
                        None,
                    )
                    if target_sym
                    else None
                )
                if source_mod and target_mod and source_mod.semantic_key != target_mod.semantic_key:
                    import_edges.append(
                        GraphEdge(
                            id=f"{source_mod.semantic_key}->{target_mod.semantic_key}",
                            source=source_mod.semantic_key,
                            target=target_mod.semantic_key,
                            relation="imports",
                        )
                    )

    components_sections: list[ComponentSectionV1] = []
    if module_nodes:
        import_proj = build_projection(module_nodes, import_edges, relation_family="imports")
        cond_res = condense(import_proj)
        components_sections.append(
            ComponentSectionV1(
                relation="imports",
                state="complete" if cond_res.disposition == "complete" else "partial",
                components=cond_res.components or (),
                edges=cond_res.edges or (),
                scanned_edges=cond_res.scanned_edges,
                reason=cond_res.reason,
            )
        )

    # 6. Reconcile coverage
    eligible_count = sum(1 for f in source_input.files if f.eligible)
    excluded_count = sum(1 for f in source_input.files if not f.eligible)

    out_of_scope_count = 0
    unsupported_count = 0
    for f in source_input.files:
        if not f.eligible:
            continue
        in_scope = not source_input.config.include_prefixes or any(
            f.path == p or f.path.startswith(p + "/") for p in source_input.config.include_prefixes
        )
        if not in_scope:
            out_of_scope_count += 1
            continue
        is_lang = Path(f.path).suffix.casefold() in DIALECTS
        is_cfg = Path(f.path).name == "package.json" or (
            Path(f.path).name.startswith("tsconfig") and f.path.endswith((".json", ".jsonc"))
        )
        if not is_lang and not is_cfg:
            unsupported_count += 1

    resolved_count = sum(1 for r in resolutions if r.status == "resolved")
    ambiguous_count = sum(1 for r in resolutions if r.status == "ambiguous")
    unresolved_count = sum(1 for r in resolutions if r.status == "unresolved")

    omissions = tuple(
        FileOmissionV1(file_id=files_by_path[p].file_id, path=p, reason=r)
        for p, r in reader.omitted
        if p in files_by_path
    )

    coverage = StructuralCoverageV1(
        requested_files=reader.requested,
        processed_files=len(units),
        omitted_files=len(reader.omitted),
        inventoried_files=len(source_input.files),
        eligible_files=eligible_count,
        excluded_files=excluded_count,
        unsupported_files=unsupported_count,
        out_of_scope_files=out_of_scope_count,
        resolution_records=len(resolutions),
        resolved=resolved_count,
        ambiguous=ambiguous_count,
        unresolved=unresolved_count,
        distinct_resolution_sites=len({r.site_key for r in resolutions}),
        omitted_diagnostics=max(0, len(all_diagnostics) - limits.diagnostics),
        syntax="partial"
        if reader.omitted or any(f.has_syntax_errors for f in files_obs)
        else "complete",
        resolution="partial" if ts_incomplete or resolution_truncated else "complete",
        framework="partial" if framework_truncated else "complete",
        graph="partial" if reader.omitted or resolution_truncated else "complete",
        omissions=omissions,
    )

    return StructuralDraftV1(
        schema_version=1,
        request_nonce=source_input.request_nonce,
        config_hash=source_input.config_hash,
        toolchain_hash=source_input.toolchain_hash,
        archive_sha256=source_input.archive.sha256,
        policy_hash=source_input.policy.policy_hash,
        excerpts=(),
        symbols=tuple(all_symbols[:1000]),
        resolutions=tuple(resolutions[:2000]),
        routes=tuple(routes[:256]),
        requests=tuple(http_requests[:256]),
        links=tuple(links[:512]),
        diagnostics=tuple(all_diagnostics[:500]),
        components=tuple(components_sections[:2]),
        coverage=coverage,
    )
