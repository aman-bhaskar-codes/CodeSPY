"""Validate untrusted drafts and materialize a complete bounded Phase 1 result."""

import hashlib
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import uuid5

from app.analyzers.identities import normalize_report, observation_key
from app.analyzers.positions import SourceIndex
from app.analyzers.result_shape import draft_locations, location_key
from app.analyzers.source import DIALECTS
from app.contracts.evidence import EvidenceEnvelopeV1
from app.contracts.persistence import CoverageV1, EdgeCreateV1, NodeCreateV1, ResultBatchV1
from app.contracts.structure import (
    ArchitectureReportV1,
    IdentityMapV1,
    SourceExcerptV1,
    SourceInputV1,
    SourceLocationV1,
    StructuralDraftV1,
    StructuralResultV1,
)
from app.domain.canonical_json import canonical_bytes, semantic_digest
from app.domain.scope import RunRef


def materialize_result(
    ref: RunRef, draft: StructuralDraftV1, source: SourceInputV1, root: Path, captured_at: datetime
) -> StructuralResultV1:
    draft = StructuralDraftV1.model_validate(draft.model_dump())
    if (
        draft.request_nonce != source.request_nonce
        or draft.config_hash != source.config_hash
        or draft.toolchain_hash != source.toolchain_hash
        or draft.archive_sha256 != source.archive.sha256
        or draft.policy_hash != source.policy.policy_hash
    ):
        raise ValueError("draft source identity differs")
    symbols = {s.semantic_key: s for s in draft.symbols}
    if len(symbols) != len(draft.symbols):
        raise ValueError("duplicate symbol identity")
    locations = draft_locations(draft)
    limits = source.config.limits
    if len(locations) > limits.evidence or len(symbols) > limits.nodes:
        raise ValueError("result capacity exceeded")
    files = {f.file_id: f for f in source.files}
    requested = {
        f.file_id
        for f in source.files
        if f.eligible
        and Path(f.path).suffix.casefold() in DIALECTS
        and (
            not source.config.include_prefixes
            or any(
                f.path == p or f.path.startswith(p + "/") for p in source.config.include_prefixes
            )
        )
    }
    processed = {s.file_id for s in draft.symbols if s.kind == "module"}
    omitted = {o.file_id for o in draft.coverage.omissions}
    if (
        draft.coverage.inventoried_files != len(source.files)
        or draft.coverage.eligible_files != sum(f.eligible for f in source.files)
        or draft.coverage.excluded_files != sum(not f.eligible for f in source.files)
        or draft.coverage.requested_files != len(requested)
        or draft.coverage.processed_files != len(processed)
        or draft.coverage.omitted_files != len(requested - processed)
        or draft.coverage.omitted_files != len(omitted) + draft.coverage.omitted_details_count
        or len(omitted) != len(draft.coverage.omissions)
        or not processed <= requested
        or not omitted <= requested - processed
        or any(
            o.file_id not in files or o.path != files[o.file_id].path
            for o in draft.coverage.omissions
        )
        or draft.coverage.resolution_records != len(draft.resolutions)
        or draft.coverage.resolved != sum(r.status == "resolved" for r in draft.resolutions)
        or draft.coverage.ambiguous != sum(r.status == "ambiguous" for r in draft.resolutions)
        or draft.coverage.unresolved != sum(r.status == "unresolved" for r in draft.resolutions)
    ):
        raise ValueError("draft coverage differs from source identity")
    required = {loc.file_id for loc in locations}
    if not required <= files.keys():
        raise ValueError("foreign source identity")
    # Broker validation reads only bounded cited entries, never parses or executes target code.
    # Archive and manifest bytes were independently verified by the object-store stream.
    indexes = {}
    total = 0
    with zipfile.ZipFile(root / "archive.zip") as archive:
        for file_id in sorted(required, key=str):
            file = files[file_id]
            if (
                not file.eligible
                or file.size_bytes > limits.file_bytes
                or (
                    source.config.include_prefixes
                    and not any(
                        file.path == p or file.path.startswith(p + "/")
                        for p in source.config.include_prefixes
                    )
                )
            ):
                raise ValueError("excluded source identity")
            matches = [
                i
                for i in archive.infolist()
                if not i.is_dir()
                and (
                    i.filename.split("/", 1)[-1] if source.policy.strip_single_root else i.filename
                )
                == file.path
            ]
            if len(matches) != 1 or matches[0].file_size != file.size_bytes:
                raise ValueError("source identity differs")
            total += file.size_bytes
            if total > limits.source_bytes:
                raise ValueError("source capacity exceeded")
            with archive.open(matches[0]) as stream:
                data = stream.read(file.size_bytes + 1)
            if len(data) != file.size_bytes or hashlib.sha256(data).hexdigest() != file.sha256:
                raise ValueError("source identity differs")
            indexes[file_id] = SourceIndex(data)
    for loc in locations:
        if loc.sha256 != files[loc.file_id].sha256:
            raise ValueError("source identity differs")
        indexes[loc.file_id].validate_range(loc.range)
    for symbol in symbols.values():
        if (
            symbol.path != files[symbol.file_id].path
            or symbol.sha256 != symbol.location.sha256
            or observation_key(symbol) != symbol.semantic_key
            or (symbol.container_key is not None and symbol.container_key not in symbols)
        ):
            raise ValueError("symbol identity or containment differs")
    for resolution in draft.resolutions:
        if (
            resolution.site_key not in symbols
            or not set(resolution.candidate_keys) <= symbols.keys()
        ):
            raise ValueError("foreign resolution identity")
    routes = {r.key for r in draft.routes}
    requests = {r.key for r in draft.requests}
    for link in draft.links:
        if link.request_key not in requests or not set(link.route_keys) <= routes:
            raise ValueError("foreign boundary identity")
    for component in draft.components:
        if any(key not in symbols for group in component.components for key in group):
            raise ValueError("foreign component identity")
    for excerpt in draft.excerpts:
        if excerpt.location not in locations:
            raise ValueError("foreign excerpt identity")
        span = excerpt.location.range
        if (
            indexes[excerpt.location.file_id].data[span.start_byte : span.end_byte]
            != excerpt.text.encode()
        ):
            raise ValueError("excerpt differs from source")
    node_ids = {key: uuid5(ref.run_id, "symbol:" + key) for key in symbols}
    evidence_ids = {
        location_key(loc): uuid5(ref.run_id, "evidence:" + location_key(loc)) for loc in locations
    }
    nodes = [
        NodeCreateV1(
            local_id=node_ids[key],
            file_id=s.file_id,
            node_kind=s.kind,
            symbol_name=s.name,
            qualified_name=s.qualified_name,
            range=s.location.range,
            attributes={"semantic_key": key, "role": s.role, "type_only": s.type_only},
        )
        for key, s in sorted(symbols.items())
    ]
    evidence = [
        EvidenceEnvelopeV1(
            scope=ref,
            id=evidence_ids[location_key(loc)],
            file_id=loc.file_id,
            range=loc.range,
            provider_name="codespy-structure",
            provider_version="1",
            operation="analyze_structure",
            observation_mode="static",
            basis="observed",
            coverage_status="complete",
            payload={"source_sha256": loc.sha256, "toolchain_hash": source.toolchain_hash},
            captured_at=captured_at,
        )
        for loc in locations
    ]
    edges = {}

    def edge(
        a: str,
        b: str,
        kind: Literal["AST_CHILD", "IMPORTS", "CALLS_CANDIDATE"],
        support: SourceLocationV1,
    ) -> None:
        key = semantic_digest([a, b, kind, location_key(support)])
        edges[key] = EdgeCreateV1(
            id=uuid5(ref.run_id, "edge:" + key),
            source_local_id=node_ids[a],
            target_local_id=node_ids[b],
            edge_kind=kind,
            support_evidence_id=evidence_ids[location_key(support)],
        )

    for key, symbol in sorted(symbols.items()):
        if symbol.container_key:
            edge(symbol.container_key, key, "AST_CHILD", symbol.location)
    for resolution in draft.resolutions:
        site = symbols[resolution.site_key]
        if resolution.resolution_kind == "import" and not site.type_only:
            for candidate in resolution.candidate_keys:
                edge(site.semantic_key, candidate, "IMPORTS", site.location)
        elif resolution.resolution_kind == "call_target":
            for candidate in resolution.candidate_keys:
                edge(site.semantic_key, candidate, "CALLS_CANDIDATE", site.location)
    if len(edges) > limits.edges:
        raise ValueError("edge capacity exceeded")
    partial = (
        draft.coverage.omitted_files > 0
        or draft.coverage.unresolved > 0
        or draft.coverage.omitted_diagnostics > 0
        or any(
            getattr(draft.coverage, name) != "complete"
            for name in ("syntax", "resolution", "framework", "graph")
        )
    )
    batch = ResultBatchV1(
        scope=ref,
        nodes=nodes,
        evidence=evidence,
        edges=[edges[k] for k in sorted(edges)],
        disposition="partial" if partial else "completed",
        coverage=CoverageV1(
            status="partial" if partial else "complete",
            scope_description="Static structural observations within structural-small-v1",
            admitted_count=draft.coverage.requested_files,
            processed_count=draft.coverage.processed_files,
            omitted_count=draft.coverage.omitted_files,
            truncated=draft.coverage.omitted_files > 0,
        ),
        limitations=["Static bindings and candidate edges do not prove runtime reachability."],
    )
    excerpts = []
    size = 0
    for loc in locations:
        data = indexes[loc.file_id].data[loc.range.start_byte : loc.range.end_byte]
        if len(data) <= 2048 and size + len(data) <= 65536:
            excerpts.append(
                SourceExcerptV1(
                    location=loc, text=data.decode(), sha256=hashlib.sha256(data).hexdigest()
                )
            )
            size += len(data)
    report = ArchitectureReportV1(
        **draft.model_dump(exclude={"excerpts"}),
        run_ref=ref,
        limits=limits,
        symbol_map=tuple(
            IdentityMapV1(semantic_key=k, physical_id=v) for k, v in sorted(node_ids.items())
        ),
        evidence_map=tuple(
            IdentityMapV1(semantic_key=k, physical_id=v) for k, v in sorted(evidence_ids.items())
        ),
        excerpts=tuple(excerpts),
    )
    result = StructuralResultV1(
        run_ref=ref,
        batch=batch,
        report=report,
        report_sha256=semantic_digest(report.model_dump()),
        normalized_sha256=semantic_digest(normalize_report(report)),
    )
    if len(canonical_bytes(result.model_dump())) > limits.result_bytes:
        raise ValueError("result byte capacity exceeded")
    return result
