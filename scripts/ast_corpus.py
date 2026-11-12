"""Resolve, fetch and measure the pinned AST corpus (live GitHub access).

uv run --project backend python scripts/ast_corpus.py resolve|fetch|run [--json PATH]
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "backend")]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("resolve", "fetch", "run"))
    parser.add_argument("--json", type=Path, help="write the run report here")
    args = parser.parse_args()
    from evals.ast import fetch, runner

    if args.command == "resolve":
        asyncio.run(fetch.resolve())
    elif args.command == "fetch":
        asyncio.run(fetch.fetch())
    else:
        report = runner.run_corpus(asyncio.run(fetch.fetch()))
        text = json.dumps(report, indent=2)
        if args.json:
            args.json.write_text(text + "\n")
        print(runner.render(report))


if __name__ == "__main__":
    main()
