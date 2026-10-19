"""Fixed Linux analyzer entrypoint. Input never selects a command or imports target code."""

import hashlib
import json
import resource
import signal
import sys
from pathlib import Path

from app.analyzers.pipeline import analyze
from app.contracts.structure import SourceInputV1
from app.domain.canonical_json import canonical_bytes


def main() -> None:
    if sys.argv[1:] == ["export"]:
        with Path("/output/draft.json").open("rb") as stream:
            while block := stream.read(65536):
                sys.stdout.buffer.write(block)
        return
    if sys.argv[1:]:
        raise ValueError("fixed analyzer command required")
    raw = Path("/input/request.json").read_bytes()
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("input capacity")
    source = SourceInputV1.model_validate_json(raw)
    if source.toolchain_hash != Path("/opt/codespy/toolchain.sha256").read_text().strip():
        raise ValueError("toolchain identity differs")
    draft = analyze(source, Path("/input"), isolated_operations=True)
    output = canonical_bytes(draft.model_dump())
    if len(output) > 16 * 1024 * 1024:
        raise ValueError("output capacity")
    Path("/output/draft.json").write_bytes(output)
    peak = Path("/sys/fs/cgroup/memory.peak")
    memory_peak = int(peak.read_text().strip()) if peak.exists() else None
    print(
        json.dumps(
            {
                "sha256": hashlib.sha256(output).hexdigest(),
                "rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                "cgroup_peak_bytes": memory_peak,
            }
        ),
        flush=True,
    )
    signal.pause()  # Owner removes the container after bounded output transfer.


if __name__ == "__main__":
    main()
