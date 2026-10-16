# ADR 0001: setup boundaries

- No application code, migrations, DSA implementation, agents, UI, deployment, or fine-tuning during setup.
- Choose Celery (Redis locally, SQS in AWS); do not install competing Arq orchestration.
- Choose Terraform-compatible HCL with OpenTofu for future AWS IaC, not both Terraform and CDK. No resource declarations until account/region/cost scope is known.
- Retain both graph UI libraries for evaluation; choose one during frontend design.
- tsvector/GIN with ts_rank is PostgreSQL full-text search, not BM25. True BM25 needs an explicit implementation or compatible extension; RDS compatibility must be checked.
- Gemini 2.5 Pro documentation lists a 1,048,576-token input limit. Do not assume a 2M window. Model IDs remain account-verified configuration, not hardcoded promises.
- Firecracker requires Linux/KVM. Treat it as a separate worker-host decision, not an ordinary Fargate container capability.
- RDS Multi-AZ and PITR reduce risk but do not guarantee zero data loss; define RPO/RTO and demonstrate restores later.
- Recall, latency, token savings, and algorithmic complexity claims in the PDF are goals requiring measured validation.
- Fine-tuning/GPU dependencies are deferred to a separate Linux/CUDA environment; downloading model weights onto this Mac is not required for setup.

Sources: https://www.postgresql.org/docs/16/textsearch-controls.html ; https://ai.google.dev/gemini-api/docs/models/gemini-2.5-pro ; https://github.com/firecracker-microvm/firecracker/blob/main/docs/getting-started.md
