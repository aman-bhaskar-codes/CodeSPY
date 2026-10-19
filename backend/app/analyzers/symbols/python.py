"""Python lexical and local-import resolution without target code execution."""

import ast
from collections.abc import Mapping, Sequence

from app.contracts.structure import (
    AnalysisBudget,
    AnalysisConfigV1,
    DiagnosticV1,
    FileObservationsV1,
    ResolutionV1,
    SourceUnitV1,
    SymbolObservationV1,
)


def _resolve_module_path(
    current_path: str, module_name: str | None, level: int, sources: Mapping[str, SourceUnitV1]
) -> str | None:
    """Resolve relative or absolute local module path against admitted sources."""
    current_dir = current_path.rsplit("/", 1)[0] if "/" in current_path else ""

    if level > 0:
        # Relative import
        parts = current_dir.split("/") if current_dir else []
        if level - 1 > len(parts):
            return None
        base_dir = "/".join(parts[: len(parts) - (level - 1)]) if level - 1 > 0 else current_dir
        if module_name:
            rel = module_name.replace(".", "/")
            candidate_base = f"{base_dir}/{rel}" if base_dir else rel
        else:
            candidate_base = base_dir
    else:
        # Absolute import against root
        if not module_name:
            return None
        candidate_base = module_name.replace(".", "/")

    # Check candidates: candidate_base.py or candidate_base/__init__.py
    file_cand = f"{candidate_base}.py"
    pkg_cand = f"{candidate_base}/__init__.py"

    if file_cand in sources:
        return file_cand
    if pkg_cand in sources:
        return pkg_cand
    return None


def resolve_python(
    files: Sequence[FileObservationsV1],
    sources: Mapping[str, SourceUnitV1],
    config: AnalysisConfigV1,
    budget: AnalysisBudget,
) -> tuple[tuple[ResolutionV1, ...], tuple[DiagnosticV1, ...]]:
    resolutions: list[ResolutionV1] = []
    diagnostics: list[DiagnosticV1] = []

    # Map path -> list of SymbolObservationV1
    symbol_map: dict[str, list[SymbolObservationV1]] = {f.path: list(f.symbols) for f in files}

    for file_obs in files:
        if file_obs.language != "python":
            continue

        unit = sources.get(file_obs.path)
        if not unit:
            continue

        try:
            tree = ast.parse(unit.data)
        except (SyntaxError, ValueError):
            diagnostics.append(
                DiagnosticV1(
                    code="python_ast_parse_failure",
                    severity="warning",
                    message="Python ast parser could not supplement tree-sitter observations",
                    stage="resolution",
                )
            )
            continue

        # Find local declarations in this file
        local_symbols_by_name: dict[str, SymbolObservationV1] = {}
        for s in file_obs.symbols:
            if s.role == "declaration" and s.kind in ("function", "class"):
                local_symbols_by_name[s.name] = s

        # Find imported names: imported_alias -> (target_path, target_name)
        imports_by_alias: dict[str, tuple[str, str | None]] = {}
        for node in ast.walk(tree):
            budget.tick_nodes(1)
            if isinstance(node, ast.Import):
                for alias in node.names:
                    target_path = _resolve_module_path(unit.path, alias.name, 0, sources)
                    if target_path:
                        name = alias.asname or alias.name
                        imports_by_alias[name] = (target_path, None)
            elif isinstance(node, ast.ImportFrom):
                target_path = _resolve_module_path(unit.path, node.module, node.level, sources)
                if target_path:
                    for alias in node.names:
                        name = alias.asname or alias.name
                        imports_by_alias[name] = (target_path, alias.name)

        # Match symbols from FileObservationsV1
        for sym in file_obs.symbols:
            if len(resolutions) >= min(config.limits.resolution_sites, 2000):
                diagnostics.append(
                    DiagnosticV1(
                        code="resolution_site_budget_reached",
                        severity="info",
                        message="reached maximum resolution sites budget",
                        stage="resolution",
                    )
                )
                break

            if sym.role == "import":
                # Check if import matches a known local target
                target_cand: SymbolObservationV1 | None = None
                for alias_name, (target_path, target_symbol_name) in imports_by_alias.items():
                    matched = alias_name in sym.name or (
                        target_symbol_name is not None and target_symbol_name in sym.name
                    )
                    if matched:
                        target_syms = symbol_map.get(target_path, [])
                        if target_symbol_name:
                            target_cand = next(
                                (s for s in target_syms if s.name == target_symbol_name), None
                            )
                        else:
                            target_cand = next((s for s in target_syms if s.kind == "module"), None)
                        if target_cand:
                            break

                if target_cand:
                    resolutions.append(
                        ResolutionV1(
                            site_key=sym.semantic_key,
                            resolution_kind="import",
                            status="resolved",
                            candidate_keys=(target_cand.semantic_key,),
                            basis="import_alias",
                            reason="local_import_resolved",
                        )
                    )
                else:
                    resolutions.append(
                        ResolutionV1(
                            site_key=sym.semantic_key,
                            resolution_kind="import",
                            status="unresolved",
                            candidate_keys=(),
                            basis="unsupported",
                            reason="external_or_unresolved_import",
                        )
                    )

            elif sym.role == "call":
                # Check if direct call
                call_name = sym.name
                target_sym: SymbolObservationV1 | None = None

                if "." not in call_name:
                    # Direct call: check local declaration
                    if call_name in local_symbols_by_name:
                        target_sym = local_symbols_by_name[call_name]
                        resolutions.append(
                            ResolutionV1(
                                site_key=sym.semantic_key,
                                resolution_kind="call_target",
                                status="resolved",
                                candidate_keys=(target_sym.semantic_key,),
                                basis="lexical",
                                reason="local_function_or_class",
                            )
                        )
                        continue
                    # Check imported name
                    if call_name in imports_by_alias:
                        tpath, tsym = imports_by_alias[call_name]
                        target_syms = symbol_map.get(tpath, [])
                        if tsym:
                            target_sym = next((s for s in target_syms if s.name == tsym), None)
                        if target_sym:
                            resolutions.append(
                                ResolutionV1(
                                    site_key=sym.semantic_key,
                                    resolution_kind="call_target",
                                    status="resolved",
                                    candidate_keys=(target_sym.semantic_key,),
                                    basis="import_alias",
                                    reason="imported_callable",
                                )
                            )
                            continue
                else:
                    # Attribute call: e.g. mod.func()
                    receiver, method_name = call_name.split(".", 1)
                    if receiver in imports_by_alias:
                        tpath, _ = imports_by_alias[receiver]
                        target_syms = symbol_map.get(tpath, [])
                        target_sym = next((s for s in target_syms if s.name == method_name), None)
                        if target_sym:
                            resolutions.append(
                                ResolutionV1(
                                    site_key=sym.semantic_key,
                                    resolution_kind="call_target",
                                    status="resolved",
                                    candidate_keys=(target_sym.semantic_key,),
                                    basis="static_member",
                                    reason="module_member_call",
                                )
                            )
                            continue

                # Unresolved call
                resolutions.append(
                    ResolutionV1(
                        site_key=sym.semantic_key,
                        resolution_kind="call_target",
                        status="unresolved",
                        candidate_keys=(),
                        basis="unsupported",
                        reason="dynamic_or_unresolved_receiver",
                    )
                )

    return tuple(resolutions), tuple(diagnostics[: min(config.limits.diagnostics, 500)])
