"""Measure the architecture report over the pinned corpus: resolution quality, structure, cost.

Facts are extracted in-process over every eligible file (the production 128-file cap is the shard
planner's concern, measured separately), then the same assembly the service uses builds the report.
Floors in ``corpus.json`` (``report_floors``) fail a run that regresses resolution or growth.
"""

import io
import time
import zipfile
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.admission.zip_inventory import inspect_zip_bytes
from app.analyzers.facts import FACT_LANGUAGES, extract_facts
from app.analyzers.graph import FileEntry
from app.analyzers.graph.analytics import analyze
from app.analyzers.graph.render import render_markdown
from app.analyzers.graph.report import assemble_report
from app.analyzers.graph.shards import plan_shards
from app.analyzers.registry import FACT_DIALECTS
from app.contracts.ingestion import AdmissionPolicyV3
from app.contracts.structure import AnalysisBudget, SourceUnitV1

SECTIONS = 8


def load_entries(path: Path) -> tuple[list[FileEntry], list[str], dict[str, int]]:
    data = path.read_bytes()
    manifest = inspect_zip_bytes(data, AdmissionPolicyV3(github_strip_root=True))
    known = {e.path: e for e in manifest.entries}
    budget = AnalysisBudget(wall_seconds=3600.0, max_syntax_nodes=10**9)
    entries: list[FileEntry] = []
    inventory: list[str] = []
    sizes: dict[str, int] = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for info in archive.infolist():
            if info.is_dir() or "/" not in info.filename:
                continue
            rel = info.filename.split("/", 1)[1]
            entry = known.get(rel)
            if entry is None or not entry.eligible:
                continue
            inventory.append(rel)
            language = FACT_DIALECTS.get(Path(rel).suffix.casefold())
            if language not in FACT_LANGUAGES:
                continue
            raw = archive.read(info)
            sizes[rel] = len(raw)
            unit = SourceUnitV1(
                file_id=uuid4(), path=rel, sha256=entry.sha256, data=raw, language=language
            )
            entries.append(FileEntry(unit.file_id, rel, language, extract_facts(unit, budget)))
    return entries, inventory, sizes


def measure_report(name: str, commit: str, path: Path) -> dict[str, Any]:
    entries, inventory, sizes = load_entries(path)
    ids = (uuid4(), uuid4(), uuid4())
    started = time.perf_counter()
    report = assemble_report(
        org_id=ids[0],
        repository_id=ids[1],
        snapshot_id=ids[2],
        files=entries,
        inventory=inventory,
        eligible_files=len(entries),
        omitted_files=0,
        fact_runs=(uuid4(),),
    )
    seconds = time.perf_counter() - started
    counts = report.graph.counts()
    internal = counts["resolved"] + counts["ambiguous"] + counts["unresolved"]
    analytics = analyze(report.graph)
    plan = plan_shards(sizes)
    return {
        "name": name,
        "commit": commit,
        "files": len(entries),
        "edges": sum(counts.values()),
        "resolved": counts["resolved"],
        "ambiguous": counts["ambiguous"],
        "external": counts["external"],
        "unresolved": counts["unresolved"],
        "unresolved_permille": 1000 * counts["unresolved"] // max(1, internal),
        "ambiguous_permille": 1000 * counts["ambiguous"] // max(1, internal),
        "components": len(analytics.components),
        "file_cycles": len(analytics.file_cycles),
        "component_cycles": len(analytics.component_cycles),
        "sections": len(report.sections),
        "report_kib": report.size_bytes() // 1024,
        "markdown_kib": len(render_markdown(report)) // 1024,
        "seconds": round(seconds, 3),
        "shards": len(plan.shards),
        "digest": report.digest(),
        "analytics": analytics.disposition,
    }


def check_report(results: list[dict[str, Any]], manifest: dict[str, Any]) -> list[str]:
    floors = {r["name"]: r.get("report_floors", {}) for r in manifest["repos"]}
    problems = []
    for result in results:
        name, expected = result["name"], floors.get(result["name"], {})
        if result["sections"] != SECTIONS:
            problems.append(f"{name}: expected {SECTIONS} sections, got {result['sections']}")
        if result["analytics"] != "complete":
            problems.append(f"{name}: analytics stopped early ({result['analytics']})")
        limits = {
            "min_resolved": ("resolved", 1),
            "max_unresolved_permille": ("unresolved_permille", -1),
            "max_ambiguous_permille": ("ambiguous_permille", -1),
            "min_components": ("components", 1),
            "max_report_kib": ("report_kib", -1),
            "max_seconds": ("seconds", -1),
        }
        for key, (metric, direction) in limits.items():
            if key in expected:
                value = result[metric]
                if (direction > 0 and value < expected[key]) or (
                    direction < 0 and value > expected[key]
                ):
                    problems.append(f"{name}: {metric} {value} violates {key} {expected[key]}")
    return problems
