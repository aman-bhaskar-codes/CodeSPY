# Portfolio demo cost policy

Owner reports $100 in AWS credits, with a possible additional $60 subject to AWS eligibility. The balance, eligibility, expiry, and eligible services have not been verified. Do not assume credits cover every charge.

Recommended planning envelope (not configured in AWS): USD 10 monthly alert budget, notifications at 50%, 80%, and 100%, plus a USD 25 cumulative demo-spend review threshold. Alerts are not spending caps. Verify credit expiry before deploying.

Start locally. Provision AWS only when an end-to-end demo can run. Select the region after account access and service/pricing checks; this document does not assume one.

Demonstration target: static frontend hosting plus a minimal on-demand API/worker footprint and PostgreSQL storage. Evaluate single-AZ RDS versus a disposable single-host PostgreSQL demo only after reviewing current costs, persistence needs, and pgvector availability. This demo is not the production HA architecture.

Defer Multi-AZ RDS, ElastiCache, always-on ALB, NAT gateways, custom domains, and Firecracker host fleets until their portfolio benefit justifies their cost. Use local Redis/SearXNG initially. Do not claim these production capabilities were deployed.

Teardown checklist: ECS tasks/services, ALB/listeners/target groups, databases/final snapshots, caches, NAT gateways, public IPv4 allocations, EBS volumes, worker instances, logs, ECR images, S3 objects/versions, hosted zones, secrets, and residual backups. Confirm billing after teardown. Never delete a shared account resource without checking ownership.

Anthropic and Gemini API charges are separate from AWS credits unless a specifically eligible AWS billing arrangement is chosen. The PDF specifies direct provider APIs. No paid inference calls were made during setup.
