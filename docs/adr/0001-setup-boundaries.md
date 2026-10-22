# ADR 0001: Dependency and runtime boundaries

Installed packages do not imply an enabled integration. Source ingestion, durable jobs and structural analysis use explicitly composed adapters; optional model, search and binary-analysis packages remain separate capabilities.

- The Python workspace has one root lockfile. Frontend and TypeScript compiler packages have independent npm locks.
- Durable scheduling uses the database outbox, SQS-compatible wakeups and database leases. Dormant Redis/Arq packages do not create a second scheduler.
- Uploaded source is data: do not import it, execute its scripts, install its packages or load its plugins.
- PostgreSQL lexical search terminology follows the implementation; tsvector/GIN with ts_rank is not BM25.
- Use Terraform-compatible HCL with OpenTofu for infrastructure definitions. Cloud activation and secrets require explicit owner authorization.
- Model context limits and availability require provider-specific configuration and qualification. Package installation is not account readiness.
- Linux/KVM capabilities are distinct from ordinary container capabilities. Container resource controls need platform-specific qualification.
- Backups and redundant database deployment do not imply zero data loss; recovery claims require observed restore evidence.
- Performance and accuracy claims require a declared corpus, platform, method and measured results.
