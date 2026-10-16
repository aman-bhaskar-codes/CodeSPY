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

## In progress

- Python package installation, Chromium runtime, native toolchain, Ghidra archive and local container images downloading.
- GitHub setup CI and AWS OIDC workflow require a first pushed commit and verification.

## Intentionally deferred or owner-dependent

- No application code, tests, migrations, agent logic, analyzers or UI implementation.
- Paid cloud compute/database/hosting provisioning deferred until working demo and cost estimate.
- API keys, OAuth registrations, embedding provider/dimensions, Langfuse endpoint and license selection pending.
- Credit balance, expiry, eligibility and billing contacts not independently verified.
- Human AWS access should migrate from root connection to an appropriate federated identity.
- GPU training/model weights remain isolated Phase 7 work.
