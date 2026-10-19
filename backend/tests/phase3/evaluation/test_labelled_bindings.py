"""Frozen synthetic held-out call sites, separate from the development unit fixtures.

These labels test declared static candidates; they are not runtime ground truth.
"""

import hashlib
import json
from pathlib import Path
from uuid import uuid4

from app.analyzers.languages import extract_syntax
from app.analyzers.symbols.python import resolve_python
from app.analyzers.symbols.typescript import resolve_typescript
from app.contracts.structure import AnalysisBudget, AnalysisConfigV1, SourceUnitV1


def test_frozen_heldout_binding_thresholds():
    labels = json.loads((Path(__file__).parent / "fixtures/heldout-v2.json").read_text())
    for language in ("python", "js_ts"):
        cases = [c for c in labels if c["language"] == language]
        units = tuple(
            SourceUnitV1(
                file_id=uuid4(),
                path=c["path"],
                data=c["source"].encode(),
                sha256=hashlib.sha256(c["source"].encode()).hexdigest(),
                language="python"
                if language == "python"
                else "typescript"
                if c["path"].endswith(".ts")
                else "javascript",
            )
            for c in cases
        )
        budget = AnalysisBudget(wall_seconds=30)
        obs = [extract_syntax(u, budget) for u in units]
        symbols = [s for f in obs for s in f.symbols]
        config = AnalysisConfigV1(toolchain_hash="a" * 64)
        rows = (
            resolve_python(obs, {u.path: u for u in units}, config, budget)[0]
            if language == "python"
            else resolve_typescript(units, symbols, [], config, budget)[0]
        )
        bysite = {r.site_key: r for r in rows if r.resolution_kind == "call_target"}
        bykey = {s.semantic_key: s for s in symbols}
        tp = fp = fn = ambiguous = ambiguous_correct = false_unique = 0
        for case in cases:
            site = next(
                s
                for s in symbols
                if s.path == case["path"]
                and s.role == "call"
                and s.location.range.start_line == case["line"]
            )
            row = bysite.get(site.semantic_key)
            correct = (
                row is not None
                and row.status == case["status"]
                and len(row.candidate_keys) == case["candidate_count"]
                and all(
                    bykey[k].name == case["candidate_name"] and bykey[k].path == case["path"]
                    for k in row.candidate_keys
                )
            )
            if case["status"] == "resolved":
                tp += int(correct)
                fn += int(not correct)
                fp += int(row is not None and row.status == "resolved" and not correct)
            elif case["status"] == "ambiguous":
                ambiguous += 1
                ambiguous_correct += int(correct)
                fp += int(row is not None and row.status == "resolved")
            else:
                false_unique += int(row is not None and row.status == "resolved")
                fp += int(row is not None and row.status == "resolved")
        precision = tp / (tp + fp) if tp + fp else 0
        recall = tp / (tp + fn) if tp + fn else 0
        print(
            {
                "language": language,
                "sites": len(cases),
                "tp": tp,
                "fp": fp,
                "fn": fn,
                "precision": precision,
                "recall": recall,
                "ambiguous_correct": ambiguous_correct,
                "ambiguous_total": ambiguous,
                "false_unique_negative": false_unique,
            }
        )
        assert precision >= 0.95 and recall >= 0.80 and ambiguous_correct / ambiguous >= 0.90
        assert false_unique == 0
