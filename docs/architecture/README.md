# Architecture setup

Phase 0 prepares tooling only. No API, ORM schema, DSA engine, agents, workbench or reconstruction capability has been implemented.

Planned boundaries: API -> application services -> repositories, workers/analyzers, CPG/DSA and graph retrieval. Providers and untrusted runtime inspection remain separate adapters. See ../PROJECT_PLAN.md for phases and ../adr for decisions.
