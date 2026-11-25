"""Build the sandbox image. Needs Docker and network access to pull the pinned base image."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TAG = "codespy-sandbox:dev"


def main() -> int:
    tag = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TAG
    return subprocess.call(
        ["docker", "build", "--tag", tag, str(ROOT / "infra/sandbox")]
    )


if __name__ == "__main__":
    raise SystemExit(main())
