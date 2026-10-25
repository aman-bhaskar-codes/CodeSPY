# Offline index preparation runtime

The request-bound correction is recorded in [Durable indexing requests and recovery](index-jobs.md). The latest image and worker qualification are recorded in [Atomic index publication](index-publication.md).

The typed builder accepts an index specification and trusted owned file
references. It revalidates the exact selected file set, admitted metadata,
regular-file ownership boundaries, size and SHA-256 before preparing fixed
read-only inputs. Paths with parent traversal or symlink components are rejected.
The worker obtains those inputs through the admitted source reader;
the builder does not accept caller-provided filesystem paths through an API.

A capability probe verifies the fixed entrypoint, runtime source fingerprint and
baked SHA-256-verified tokenizer vocabulary. Missing or corrupt vocabulary fails
before the tokenizer can attempt a download. The default implementation remains
unavailable. Worker execution requires an explicitly injected qualified builder;
no public indexing API is activated.

The container uses the existing offline profile: network disabled, read-only
root, unprivileged user, dropped capabilities, no new privileges, no Docker logs,
1 GiB memory with no additional swap allowance, two CPUs and 64 PIDs. Input
mounts are fixed, and uploaded content is never executed. Preparation is bounded
by 180 seconds; monotonic checks also reject success after synchronous receipt
validation overruns the deadline. Failure cleanup has a separate bounded grace
and retains owner records if Docker is unavailable.

Index output has a separate 24 MiB pipe profile and 64 KiB diagnostic ceiling.
The scanner's existing 2 MiB profile is unchanged. Diagnostics are counted and
discarded. A private receipt binds the full specification, draft digest, image
and runtime fingerprint. It provides in-process provenance, not bearer authority,
a defense against arbitrary broker code, or database publication authority.

## Reproducible build

The image context contains an explicit source allowlist, a pinned Python base,
the verified public vocabulary and the exact required subset of the existing
hash-locked binary dependencies. The application lockfile was not changed.
An optional complete wheelhouse is validated against locked filenames, package
membership and SHA-256 hashes before enabling offline image installation.
This is not a new dependency-age or advisory qualification of the existing lock.
No credentials, developer configuration or analyzed artifacts enter the context.

Image from the original preparation-runtime run:
`sha256:d2bc431038813895ce4742582d084a1c51a672e266697034c15c23e6009b986e`

Runtime source fingerprint:
`86e8ce0da8b601af0a16cc3c17c3f628e23ba7abbee1b5ec26f48b0379fd0203`

## Observed checks

On 24 October 2026, reference
`83767b105f0eb0870c79f84402241d729084120b` plus local changes:

- Controlled adapter, offline protocol, real host pipe and build-context tests:
  **33 passed**. Controlled controllers are not isolation qualification.
- Real Linux container checks: **seven cases passed**, across a two-case
  capacity/overflow selection and a five-case remaining-controls selection.
  They cover plain input, long lines, redaction-heavy input, 128-file capacity,
  running-container cancellation, output overflow and owner recovery.
- The synthetic 128-file input totaled exactly **16 MiB**. Preparation took
  **14.270 seconds**, retained **4,096 chunks**, and reported **partial** coverage
  for bounded omissions. Docker memory samples reached **131,386,572 bytes**.
  Sampling is not a true peak-memory measurement or a universal performance
  guarantee. The configured 1 GiB resource profile was independently inspected.
- Combined backend selection: **958 passed, 41 skipped**. Skips comprise 27
  PostgreSQL checks, seven standalone source-reader checks and seven indexer
  checks. The indexer checks ran separately as described above; PostgreSQL and
  standalone source-reader checks were not rerun for this runtime-only change.
- Strict typing passed for **219 application files**; Ruff passed.

Direct container-side dependency downloads timed out. The successful image was
built from a complete locally verified wheelhouse. One native test initially
failed from a missing test import; the corrected overflow case then passed.
The capacity case was rerun with memory sampling. Earlier failed build
containers and the prior owned PostgreSQL container/network were removed.
Final owned indexer and database container inventories were empty. The idle
image is retained for subsequent worker tests.

Durable creation/retries, consumer execution, atomic publication, sealed-set
read visibility and UI/MCP integration still require implementation. The database
finalization budget has not been qualified by these preparation tests. No cloud,
provider, deployment or end-to-end product readiness is claimed.
