# AWS setup ownership

**Owner-managed. Do not access the AWS account.** The owner explicitly stopped all assistant AWS access on 2026-10-16. This overrides the runbook, older plans and any pending sign-in. No further browser visits, real AWS CLI commands or AWS connector calls are permitted without a new explicit instruction.

## Recorded work before that instruction

The following API operations reported success before access was stopped. They have not been re-read since the stop instruction:

- Mumbai (`ap-south-1`) selected; AWS CLI v2 installed locally.
- Root MFA was observed enabled and root access keys absent.
- Approved GitHub OIDC provider and identity-only validation role were created; the earlier manual authentication workflow succeeded. No deployment rights were added to that role.
- State bucket `codespy-tfstate-<ACCOUNT_ID>-ap-south-1` created with AES256 encryption, versioning, public-access blocks and an HTTPS-only bucket policy.
- On-demand DynamoDB lock table `codespy-terraform-locks` created.
- ECR repositories `codespy-backend`, `codespy-worker`, `codespy-frontend` created with immutable tags and scan-on-push settings.
- `codespy/prod/api-keys` created as a clearly labelled placeholder containing REPLACE values; it contains no real provider credentials from this setup.
- `CodeSPY-Monthly-Dev-Budget` created with USD 25 monthly alerts at 80% and 100% to the owner-provided address; the existing USD 5 Portfolio Website budget was preserved. Both are account-level alerts, not hard spending caps.
- Reserved ECS task/execution/worker roles created with ECS trust and no attached runtime data permissions.
- After explicit approval, `codespy-aman` and `codespy-admins` were created with AdministratorAccess and the prepared MFA-required policy. No access keys or console password were created.
- Named codespy CLI profile prepared with region/output settings only. The launched short-lived login expired/cancelled without completing authentication.

No application RDS, ECS services, ElastiCache, load balancer, NAT gateway, hosting or inference deployment was performed. The recorded resources remain unchanged; the assistant did not delete or roll back resources after account access was prohibited.

## Owner's later manual work

The owner controls password/MFA enrollment, CLI authentication, provider credentials, cloud configuration review, cost/credit eligibility and eventual deployment. Remote-backend examples are in infra/terraform/environments; they are not initialized against the real account by the local verifier.

The Secrets Manager placeholder is a real AWS resource and may incur the service's normal storage charge. See https://aws.amazon.com/secrets-manager/pricing/ . No new chargeable inference or hosting has been started by this local setup.

LocalStack is separate local infrastructure and always uses an explicit loopback endpoint and dummy credentials. Local verification must never fall back to a real AWS profile.
