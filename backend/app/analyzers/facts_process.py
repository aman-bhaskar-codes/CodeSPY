"""Per-file external native deadline for fact extraction, mirroring syntax_process."""

import json
import os
import subprocess
import sys
import tempfile

from app.contracts.ast_facts import FileFactsV1
from app.contracts.structure import AnalysisBudget, SourceUnitV1


def extract_facts_isolated(unit: SourceUnitV1, budget: AnalysisBudget, seconds: int) -> FileFactsV1:
    budget.tick_nodes(0)
    remaining = budget.wall_seconds - (budget.clock() - budget.start_time)
    if remaining <= 0:
        raise TimeoutError("analysis_timeout")
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        result = subprocess.run(
            [sys.executable, "-m", "app.analyzers.facts_process"],
            input=unit.model_dump_json().encode(),
            stdout=output,
            stderr=errors,
            timeout=min(seconds, remaining),
            check=False,
            env={
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
            },
        )
        if result.returncode:
            raise ValueError("analyzer_output_invalid")
        output.seek(0)
        raw = output.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError("analyzer_output_invalid")
    envelope = json.loads(raw)
    if type(envelope.get("nodes")) is not int or not 0 <= envelope["nodes"] <= 200000:
        raise ValueError("syntax budget invalid")
    budget.tick_nodes(envelope["nodes"])
    facts = FileFactsV1.model_validate_json(json.dumps(envelope["facts"]))
    if facts.file_id != unit.file_id or facts.sha256 != unit.sha256:
        raise ValueError("source_integrity_error")
    return facts


def main() -> None:
    from app.analyzers.facts import extract_facts

    raw = sys.stdin.buffer.read(16 * 1024 * 1024 + 1)
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError("input capacity")
    unit = SourceUnitV1.model_validate_json(raw)
    budget = AnalysisBudget()
    facts = extract_facts(unit, budget)
    sys.stdout.write(
        json.dumps({"nodes": budget.nodes_visited, "facts": facts.model_dump(mode="json")})
    )


if __name__ == "__main__":
    main()
