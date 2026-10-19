"""Plan-compatible command entrypoint for the owned Phase 2 test harness."""

import sys

from phase2_db import main

if __name__ == "__main__":
    if "prepare" in sys.argv and "--env-file" in sys.argv:
        sys.argv[sys.argv.index("--env-file")] = "--env-out"
    main()
