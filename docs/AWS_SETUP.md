# AWS onboarding and production prerequisites

Status: approved GitHub OIDC provider and identity-validation role created; no database, hosting, compute, queue or storage resources provisioned. Region: ap-south-1 (Mumbai). See infra/aws/README.md for verified account findings.

Required owner inputs: target account and authenticated profile, region, monthly spend cap, budget alert recipient, repository visibility, production domain and ownership. Do not infer region from timezone.

Account formalities: verify root MFA, root access-key absence, billing contacts and alternate contacts; establish IAM Identity Center/SSO for humans; confirm CloudTrail and retention; establish budget alerts and cost allocation tags (Project=CodeSPY, Environment, Owner, CostCenter, ManagedBy). Budget alerts are not a hard spending cap.

GitHub delivery: create an OIDC provider if absent; trust only the exact repo and protected environment/ref with sts audience; separate plan and deploy roles; scope PassRole to required task roles; separate ECS execution role from application task role. Store application API keys in Secrets Manager only after account selection. Configure branch protection and environment approval rules.

Terraform state: encrypted, versioned, access-controlled S3 state, locking compatible with the selected Terraform version, separate environment keys and least-privilege state role. Never commit state or secrets.

Network: VPC with private workload/data subnets, security groups by service, explicit outbound policy and NAT/endpoint cost review. Untrusted analysis workers must be isolated from application credentials and private data services.

Planned services: Route53 and ACM; CloudFront; ALB; ECR/ECS Fargate; RDS PostgreSQL 16 with supported pgvector/pg_trgm extensions and Multi-AZ; ElastiCache Redis; encrypted S3 with versioning/lifecycle; SQS queues and DLQs; Secrets Manager; CloudWatch and OpenTelemetry. Assess Langfuse hosting separately. Firecracker requires separate Linux/KVM-capable worker infrastructure.

Before provisioning: account/region availability, quotas, RDS extensions, model availability, monthly cost estimate, tags, logging retention, TLS/domain validation, restore and rollback plan. Paid services in the plan are not collectively free.

Recovery: define numeric RPO/RTO; automated backups up to 35 days, retention/lifecycle policies, restore drills, and tested blue/green rollback. Never claim guaranteed zero loss.
