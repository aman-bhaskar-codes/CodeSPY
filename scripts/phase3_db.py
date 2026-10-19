"""Owned Phase 2 cluster lifecycle with the explicitly selected structural head."""

import sys

from scripts import phase2_db

if __name__ == "__main__":
    if "test" in sys.argv:
        if "--schema-target" not in sys.argv:
            sys.argv.extend(["--schema-target", "0011_structural_authority"])
        if "--suite" not in sys.argv:
            sys.argv.extend(["--suite", "phase3"])
    phase2_db.main()
