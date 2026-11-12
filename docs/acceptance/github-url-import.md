# Public GitHub URL import

A signed-in owner or administrator can import a public GitHub repository by URL.
The import feeds the same sandboxed admission, snapshot and inventory path as an
uploaded ZIP; no source is executed and no GitHub App installation is needed.

## Implemented boundary

`POST /api/v1/github-imports` takes `{url, ref?}` with a session cookie, CSRF token
and `Idempotency-Key`. In one transaction it creates (or reuses) an `archive`
repository keyed `github.com/<owner>/<repo>`, reserves the archive byte quota,
records a `github_public_archive` submission and enqueues an `admit_source` job.

- **URL policy.** Only `https://github.com/<owner>/<repo>[.git][/tree/<ref>]` is
  accepted. Other hosts and schemes, userinfo, ports, queries, fragments,
  percent-escapes, `www`/gist/Enterprise hosts and traversal in refs are rejected
  (HTTP 422) before any network access.
- **Fetch.** The worker resolves the ref (default branch when omitted) to one full
  40-hex commit, persists it once, then downloads the zipball for that commit over
  the fixed-origin transport (`api.github.com`, redirect only to
  `codeload.github.com`, DNS answers must all be public, no credentials or cookies,
  100 MiB cap, 120 s limit). Private or missing repositories fail as
  `repository_unavailable`; rate limits fail as retryable `provider_rate_limited`.
- **Admission.** The archive is verified into quarantine and inspected by the
  offline Linux admission container with the GitHub root-directory strip policy.
  Snapshot identity includes the pinned commit. Retries reuse the selected bytes
  and never re-resolve the commit.
- **Isolation.** The caller must be an owner or administrator of an organisation
  and gets write access only to the repository the import created.
- **Frontend.** The intake panel has a public-repository URL field that submits
  the import and follows the resulting job.
- **Local runtime.** `scripts/phase2_local.py` enables the import only when
  `CODESPY_PUBLIC_GITHUB_IMPORT=1`, because it makes live requests to GitHub.

Migration `0042_public_github_source` extends the submission-kind constraint. Its
downgrade is refused while any public-import submission exists.

## Limitations

- Repositories imported this way are `archive` repositories. A `github` repository
  requires a live GitHub App binding on every read, which public imports do not have.
- Admission limits are unchanged: 100 MiB archive, 10,000 entries, 2 MiB per eligible
  file. There is no line-count limit.
- Unauthenticated GitHub requests share GitHub's per-address rate limits.
- Private repositories still require an administrator-bound GitHub App installation,
  and that path is not wired into the local runtime or hosted configuration.
- The test suites make no live GitHub request; provider behaviour is verified with
  `httpx.MockTransport` and a fake provider. The first live use (the AST corpus) found that the
  zipball request carried no `User-Agent`, which GitHub rejects with 403; every request now sends
  `User-Agent: codespy-public-import`, covered by a unit test. The live corpus run downloaded ten
  archives through the provider.
- Importing the same URL twice reuses the repository and creates a second
  submission when the idempotency key differs.

## Verification

Run against owned disposable PostgreSQL 16 databases at schema head, with the
offline admission image built from this tree:

- URL parser accept/reject table (35 cases) and public provider unit tests with a
  mock transport: commit pin, credential-less redirect to `codeload.github.com` only,
  foreign-redirect and oversize rejection, 403/404/429/5xx classification, private
  repository refusal, and refusal of unqualified or credentialed HTTP clients
- idempotent import (one repository, one reservation, conflicting key refused),
  repository reuse, commit resolved once and bytes frozen across retries, invalid
  URL creates nothing, non-admin refused, per-tenant isolation, unavailable without
  a provider, downgrade refused with history
- API: CSRF required, 202 with a queued public submission, 409 on a differing key,
  422 for unsafe URLs, 503 without a provider
- end to end: import → real Linux admission container → sealed snapshot and
  inventory, provider root directory stripped, pinned commit recorded
- the whole `backend/tests/phase2` tree (the two catalog tests run at their own
  schema target), existing GitHub binding and acquisition tests, `ruff`, `mypy`,
  frontend typecheck, lint and the 72-test frontend suite

The unit suites made no live GitHub request; ten live downloads were made later by the corpus run.
