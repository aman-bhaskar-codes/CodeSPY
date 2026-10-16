# Owner-managed services

The owner explicitly stopped all assistant work on LLM/service API keys and all AWS account access on 2026-10-16. This instruction overrides the Phase 0 runbook.

- Do not create, generate, retrieve, configure or validate provider/API keys.
- Do not open AWS pages, sign in, or call real AWS APIs/CLI/connectors.
- The pending CLI login was cancelled/expired without completing authentication.
- The GitHub OAuth client CodeSPY-Dev was registered earlier, with the requested localhost callback; no client secret was generated.
- All API-key fields remain blank for manual owner setup. Existing local database/search/session credentials are not service API keys.
- LocalStack verification is permitted local infrastructure work and always uses explicit dummy credentials and the 127.0.0.1 endpoint; it never relies on a real AWS profile.
- Do not declare external account/key steps verified just because the local setup is complete.
