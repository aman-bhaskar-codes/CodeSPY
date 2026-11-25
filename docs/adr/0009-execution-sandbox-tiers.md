# ADR 0009: Execution sandbox tiers

Status: accepted for the local tier; the hosted tier is a recorded choice whose cloud setup is performed by the owner.
Depends on [ADR 0008](0008-agent-runtime-and-execution-policy.md).

## Decisions

1. **One port, three implementations.** A `SandboxPort` (acquire, materialise a snapshot, read, atomic write, exec,
   checkpoint, restore, suspend, release) with:
   - a fake sandbox for tests;
   - a Docker sandbox for local and self-hosted use, using the gVisor runtime when present and the scanner hardening
     flags otherwise (read-only root, all capabilities dropped, no-new-privileges, pids and memory limits, noexec tmpfs,
     default seccomp, no Docker socket, no host mounts except the workspace). The workspace persists on the host and each
     command runs in its own short-lived container, so nothing a command starts outlives it and a fork bomb or stuck
     process cannot starve later commands;
   - a hosted sandbox. The API and workers never execute workspace code.
2. **Hosted tier first choice: ECS Fargate in `ap-south-1`.** Per-task isolation boundary managed by AWS, stays in the
   owner's region, no new region to approve. Known limits: no custom runtime (so no gVisor), no suspend, cold start of
   tens of seconds (unverified), and cost while running. Lambda MicroVMs (Firecracker, suspend and resume, up to eight
   hours) is the best-fit isolation, but it is not offered in Mumbai at launch; re-evaluate when it is. Using it now in
   Tokyo would move code and runtime outside India and was declined.
3. **Network: deny by default.** No instance metadata, no route to the database. An org admin may enable egress through
   a CodeSPY-owned forward proxy with an allowlist of package registries; each install command still needs approval.
   Package installs are off until that is built.
4. **Resource profile (owner-tunable).** 2 vCPU, 4 GiB memory, 10 GiB workspace disk, 256 pids, 600 s per command,
   15 minutes idle before suspend or release, 4 hours session lifetime, non-root user, read-only root filesystem.
5. **Sandboxes are never reused across sessions** and are destroyed when a session closes.
6. **Reverse-engineering tools run in the analysis tier**, not the execution sandbox. Samples are analysed statically and
   never executed.
7. **Rejected:** running commands in the worker container (breaks isolation), WebAssembly as the execution tier (cannot
   run real toolchains such as npm, pytest or cargo), and self-managed Firecracker on bare metal (operating cost for one
   owner).

## Changes from the first draft

- The Docker sandbox runs one container per command over a persistent workspace, not one long-lived container per
  session. Suspend is therefore a no-op there (it reports that it cannot suspend). The host reads and writes the
  workspace through a symlink-safe accessor, because a command can plant links in a directory the host also uses.
- Disk limits on a bind-mounted workspace cannot be enforced by the kernel; the sandbox enforces them with a per-file
  size limit, a watchdog that stops a command that is still growing the workspace past its quota, and a check after
  every command. Hosted tiers use the platform's storage limits.

## Consequences

- The Docker implementation lets every later capability be built and verified offline; the hosted tier is exercised only
  through the owner-run checklist.
- Cost and cold-start numbers for the hosted tier are estimates until the owner measures them.
- Moving to Lambda MicroVMs later changes only the hosted adapter, not the port or the agent.

## Not verified

Fargate cold start, Lambda MicroVM egress controls and pricing were not stated in the pages reviewed.
