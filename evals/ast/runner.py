"""Measure AST fact extraction over pinned real repositories.

Admission uses policy V3 with the GitHub root strip, exactly as the importer does.
Extraction runs in-process (the same code the sandbox runs) over every eligible file, so
parser quality is measured without the production 128-file cap; the cap's effect on a
real repository is reported separately as ``production_coverage``.
"""

import io
import time
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.admission.zip_inventory import inspect_zip_bytes
from app.analyzers.facts import FACT_LANGUAGES, extract_facts
from app.analyzers.registry import FACT_DIALECTS
from app.contracts.ingestion import AdmissionPolicyV3
from app.contracts.structure import AnalysisBudget, SourceUnitV1, StructuralLimitsV1

PRODUCTION_FILES = StructuralLimitsV1().source_files


def measure(name: str, commit: str, path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    policy = AdmissionPolicyV3(github_strip_root=True)
    manifest = inspect_zip_bytes(data, policy)
    entries = {e.path: e for e in manifest.entries}
    languages: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    facts: Counter[str] = Counter()
    errors: list[dict[str, Any]] = []
    lines = error_nodes = 0
    parse_seconds = 0.0
    parsed_bytes = 0
    eligible_fact_files = 0
    budget = AnalysisBudget(wall_seconds=3600.0, max_syntax_nodes=10**9)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for info in archive.infolist():
            if info.is_dir() or "/" not in info.filename:
                continue
            rel = info.filename.split("/", 1)[1]
            entry = entries.get(rel)
            language = FACT_DIALECTS.get(Path(rel).suffix.casefold())
            if entry is None or not entry.eligible or language not in FACT_LANGUAGES:
                continue
            eligible_fact_files += 1
            raw = archive.read(info)
            unit = SourceUnitV1(
                file_id=uuid4(),
                path=rel,
                sha256=entry.sha256,
                data=raw,
                language=language,
            )
            started = time.perf_counter()
            try:
                result = extract_facts(unit, budget)
            except Exception as error:  # noqa: BLE001
                statuses["failed"] += 1
                errors.append({"path": rel, "error": f"{type(error).__name__}: {error}"[:200]})
                continue
            parse_seconds += time.perf_counter() - started
            parsed_bytes += len(raw)
            lines += raw.count(b"\n") + 1
            languages[language] += 1
            statuses[result.status] += 1
            error_nodes += result.error_nodes
            for key in ("symbols", "imports", "exports", "routes", "models", "config"):
                facts[key] += len(getattr(result, key))
    processed = sum(statuses.values())
    return {
        "name": name,
        "commit": commit,
        "archive_bytes": len(data),
        "eligible_fact_files": eligible_fact_files,
        "languages": dict(languages),
        "statuses": dict(statuses),
        "parse_success": (statuses["parsed"] + statuses["partial"]) / processed if processed else 0,
        "clean_parse_ratio": statuses["parsed"] / processed if processed else 0,
        "error_nodes": error_nodes,
        "lines": lines,
        "facts": dict(facts),
        "facts_per_kloc": {k: round(v * 1000 / lines, 2) for k, v in facts.items()}
        if lines
        else {},
        "parse_seconds": round(parse_seconds, 3),
        "files_per_second": round(processed / parse_seconds, 1) if parse_seconds else 0,
        "mib_per_second": round(parsed_bytes / 1048576 / parse_seconds, 2) if parse_seconds else 0,
        "production_coverage": round(
            min(PRODUCTION_FILES, eligible_fact_files) / eligible_fact_files, 3
        )
        if eligible_fact_files
        else 1.0,
        "failures": errors[:20],
    }


def run_corpus(fetched: list[tuple[str, str, Path, str]]) -> dict[str, Any]:
    repos = []
    for name, commit, path, digest in fetched:
        result = measure(name, commit, path)
        result["archive_sha256"] = digest
        repos.append(result)
    return {"production_file_cap": PRODUCTION_FILES, "repos": repos}


def render(report: dict[str, Any]) -> str:
    head = (
        f"{'repo':38} {'files':>6} {'ok%':>6} {'clean%':>7} "
        f"{'kloc':>7} {'f/s':>7} {'MiB/s':>6} {'cap%':>5}"
    )
    rows = [head]
    for r in report["repos"]:
        cells = (
            f"{r['name']:38} {sum(r['statuses'].values()):6}",
            f"{r['parse_success'] * 100:6.1f} {r['clean_parse_ratio'] * 100:7.1f}",
            f"{r['lines'] / 1000:7.1f} {r['files_per_second']:7} {r['mib_per_second']:6}",
            f"{r['production_coverage'] * 100:5.0f}",
        )
        rows.append(" ".join(cells))
    return "\n".join(rows)


def check(report: dict[str, Any], manifest: dict[str, Any]) -> list[str]:
    """Return one message per floor a measured repository falls below."""
    floors = {r["name"]: (r["commit"], r.get("floors", {})) for r in manifest["repos"]}
    problems = []
    for result in report["repos"]:
        commit, expected = floors[result["name"]]
        name = result["name"]
        if result["commit"] != commit:
            problems.append(f"{name}: measured commit differs from the pinned commit")
        got = {
            "min_files": sum(result["statuses"].values()),
            "min_parse_success": result["parse_success"],
            "min_clean_parse_ratio": result["clean_parse_ratio"],
            "min_symbols": result["facts"].get("symbols", 0),
            "min_routes": result["facts"].get("routes", 0),
            "min_models": result["facts"].get("models", 0),
        }
        for key, floor in expected.items():
            if got[key] < floor:
                problems.append(f"{name}: {key[4:]} {got[key]} is below the floor {floor}")
        if result["failures"]:
            problems.append(f"{name}: {len(result['failures'])} extraction crashes")
    return problems
