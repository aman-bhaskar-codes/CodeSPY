"""Owned ingestion cluster lifecycle with the explicitly selected structural head."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import phase2_db

if __name__ == "__main__":
    if "test" in sys.argv:
        if "--schema-target" not in sys.argv:
            sys.argv.extend(["--schema-target", "0011_structural_authority"])
        if "--suite" not in sys.argv:
            sys.argv.extend(["--suite", "phase3"])
    phase2_db.main()
