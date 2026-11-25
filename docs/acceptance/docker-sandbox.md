# Acceptance: Docker sandbox

Scope: the container-backed implementation of the sandbox port, for local and self-hosted use. It runs real commands,
so its tests are opt-in. Decisions are in [ADR 0009](../adr/0009-execution-sandbox-tiers.md); the port and its contract
suite are in [sandbox-port.md](sandbox-port.md).

## Design

- The workspace is a host directory bind-mounted at `/workspace`. Every command runs in its own container, removed when
  the command ends: read-only root, no network, all capabilities dropped, `no-new-privileges`, the caller's non-root user,
  bounded memory, CPU, processes and file size, a private `/tmp` and home, and gVisor (`runsc`) when the daemon has it.
  Nothing a command starts outlives it.
- The host reads and writes the workspace through `HostTree`, which walks every path one component at a time with
  `O_NOFOLLOW` and directory descriptors. A symlink, FIFO or directory swap planted by a command is never followed; links
  and special files are not files; `.git` and temporary names are invisible.
- Writes are atomic batches with rollback; a command that changes files advances the revision once; checkpoints are
  content-addressed on the host and survive a process restart, as does the sandbox itself (it re-attaches by session).
- Time and disk: a command is stopped when its time runs out or when it is still growing the workspace past its quota.
  The exit chunk says which (`timed_out`, or `limit_exceeded: "disk"`). Deleting while over quota is always allowed.

## Verification

```
python3 scripts/build_sandbox_image.py             # builds codespy-sandbox:dev (pulls the pinned base image)
cd backend
CODESPY_SANDBOX_IMAGE=codespy-sandbox:dev uv run --frozen pytest -c pyproject.toml tests/agent/sandbox -q -o addopts=
```

Without `CODESPY_SANDBOX_IMAGE` the container tests are skipped and the 54 fake and host-tree tests run.
With it: 102 tests pass, repeated three times without a failure, leaving no container or temporary directory behind.

| Guarantee                                                                                             | Test                                                       |
| ----------------------------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| The whole port contract (36 cases)                                                                    | `test_docker_contract.py`                                  |
| No network: connections to a public address and to `169.254.169.254` fail, DNS fails, no route exists | `test_there_is_no_network_at_all`                          |
| Unprivileged, no capabilities, read-only root, `no-new-privileges`                                    | `test_the_process_is_unprivileged_...`                     |
| Fork bomb stopped by the process limit; every child dies with its container; the next command works   | `test_a_fork_bomb_is_contained_...`                        |
| Disk fill stopped and reported; a single huge file cut off; deletes still allowed                     | `test_filling_the_disk_...`, `test_a_single_huge_file_...` |
| A long command is stopped and its container removed                                                   | `test_an_overlong_command_...`                             |
| A memory hog is killed without harming the sandbox                                                    | `test_a_memory_hog_...`                                    |
| Links a command plants are never followed by the host                                                 | `test_links_a_command_plants_...`, `test_hosttree.py`      |
| A restarted process re-attaches to the workspace and checkpoints                                      | `test_a_restarted_process_reattaches_...`                  |
| Release removes the workspace and every container                                                     | `test_a_released_sandbox_...`                              |

## Limits and open items

- gVisor was not available on the machine used, so isolation is plain container isolation (`isolation: "container"`).
  The adapter detects and uses `runsc` when present; that path is untested.
- The disk quota is enforced by polling, so a fast writer can overshoot by what it writes in a poll interval (the test
  stops after a few hundred megabytes); hosted tiers use hard platform limits.
- A workspace over 100,000 files is refused (`SandboxLimit`); a large dependency tree can reach that.
- `.git` is not part of the workspace view. Git tools arrive with the git capability and need git in the image; the
  current image is the pinned Python base only.
- Network egress through a registry proxy is not implemented; a spec that asks for it is refused.
