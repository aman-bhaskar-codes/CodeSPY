# Verified source reading

Source views use `SourceReaderPort`. The API never opens uploaded ZIP files.
`DockerSourceReader` streams a verified archive into an owned private staging
directory, runs a pinned minimal image offline, verifies the complete returned
file's size/digest and removes the owned container and staging files.

The source-view service requires a sealed admitted snapshot and an eligible
persisted file identity. It rechecks retained source availability and current
membership/grant after the reader returns. Original representations require the
existing original-view policy; model-safe representations retain the privacy
transform and provenance contract. Missing runtime configuration returns HTTP
503 `source_reader_unavailable`; there is no in-process extraction fallback.

## Bounds

- Archive: at most 100 MiB; its descriptor size and SHA-256 must match.
- ZIP metadata: existing admission preflight bounds and safe-path rules.
- File: at most 2 MiB, with exact persisted size and SHA-256.
- Stream transfer: at most 2 MiB stdout, bounded diagnostics; source diagnostics
  are never returned to the caller.
- Read deadline: 20 seconds, with separately bounded owned cleanup.
- Container: no network, non-root, read-only filesystem, dropped capabilities,
  no new privileges, 1 GiB memory/swap, 2 CPU, 64 PIDs and bounded temporary space.

Duplicate/colliding paths, links/special files, encrypted entries, unsupported
compression, malformed archives, missing entries and identity mismatches fail
closed. A GitHub archive's single root is stripped only according to the
persisted admission policy. A byte range is selected after full-file verification
and privacy transformation by the existing source-view representation contract.
Uploaded code is never executed and uploaded dependencies are never installed.

## Runtime composition

Build the minimal image with `scripts/build_source_reader_image.py --image-out
<new-owned-output-path>`. The build uses a whitelisted context and the exact
hash-locked Pydantic dependency closure; it never sends the full checkout or
`.env` to Docker. Configure a local immutable `sha256:` image ID, not a tag.

Construct `DockerController` with explicit Docker executable/socket paths and
an empty owned CLI configuration directory. Construct a `ScannerOwnership`
workspace for this reader and a `DockerSourceReader(owner, controller, store)`.
Pass the reader through `ApplicationPorts(source_reader=reader, ...)`. These
existing container-control names do not add a scanner or invoke target tools.

If restoring an existing workspace, validate it with `ScannerOwnership.restore`
and call `reader.recover_owned()` during exclusive startup, before accepting any
reads. Do not recover a workspace concurrently serving reads. Each API process
must own a distinct workspace. The composition root is responsible for startup
recovery; the adapter deliberately cannot discover or remove unrelated Docker
resources. Retain owner records if cleanup is uncertain, so restart can retry.

The API's default construction intentionally leaves this optional port
unconfigured. The host/bootstrap must explicitly supply the qualified runtime;
building an image alone does not enable source views.

## Qualification boundaries

See [reader acceptance](acceptance/source-reading.md) for actual commands and
results. Controlled-controller tests are not native isolation evidence.
Container and database race tests use synthetic archives and owned disposable
resources. No hosted runtime, private repository or model provider was used.
