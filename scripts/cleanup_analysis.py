"""Retry cleanup using exact persisted analyzer ownership records, never name prefixes."""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.analyzers.runner import DockerAnalysisRunner


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    runner = DockerAnalysisRunner.restore_owned(args.workspace)
    asyncio.run(runner.cleanup_pending())
    print("Matching owned analyzer containers removed; workspace record retained.")


if __name__ == "__main__":
    main()
