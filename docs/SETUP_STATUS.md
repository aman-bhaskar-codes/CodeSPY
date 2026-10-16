# Setup status

Date: 2026-10-16. Scope: professional setup only; no application implementation.

## Completed and verified

- Read all eight pages of the supplied plan and mapped phases 0-7 into docs.
- Created the public CodeSPY repository, enabled dependency alerts/updates, confirmed secret scanning and push protection.
- Initialized isolated main branch and local GitHub noreply commit identity.
- Downloaded project-local CPython 3.12.12; generated uv.lock.
- Installed frontend packages and exact direct versions; npm audit reports zero vulnerabilities after DOMPurify patching.
- Generated ignored local credentials; confirmed .env, .venv, node_modules are ignored.
- Docker Desktop 29.0.1 running; Compose config validation passed.
- Created GitHub OIDC provider and /codespy/CodeSPYGitHubValidation role after explicit approval; read back exact main-only trust and identity-only inline permission.
- Configured GitHub AWS role secret and Mumbai region; manual workflow prepared.
- Verified AWS root MFA enabled and no root access keys. Existing USD 5 account budget preserved.

## Additional verification completed

- Installed the Python environment; `uv pip check` passed and pip-audit reports no known vulnerabilities.
- Python and Node Playwright both launched Chromium successfully.
- Core SDK imports, Tree-sitter parsing and libmagic passed smoke checks.
- Semgrep 1.181.0 is isolated from application packages; native tool versions verified in TOOLCHAIN.md.
- Ghidra 12.1.4 official archive checksum matched; headless launcher printed expected usage (exit 1 with no analysis target).
- PostgreSQL 16 is healthy with vector 0.8.7 and pg_trgm 1.6; Redis PONG; SearXNG config endpoint HTTP 200.
- Container digests pinned; local Git hooks pass; committed-history gitleaks scan found no leaks.
- GitHub setup CI and actual main-branch AWS OIDC authentication succeeded.

## Final handover checks completed

- AWS OpenTofu provider 6.69.1 downloaded and signature-verified; lockfile generated. AWS CLI v2 installed; exact version recorded in TOOLCHAIN.md.
- The setup CI and AWS OIDC workflows passed; final configuration changes are published with the handover.

## Intentionally deferred or owner-dependent

- No application code, tests, migrations, agent logic, analyzers or UI implementation.
- Paid cloud compute/database/hosting provisioning deferred until working demo and cost estimate.
- API keys, OAuth registrations, embedding provider/dimensions, Langfuse endpoint and license selection pending.
- Credit balance, expiry, eligibility and billing contacts not independently verified.
- Human AWS access should migrate from root connection to an appropriate federated identity.
- GPU training/model weights remain isolated Phase 7 work.
