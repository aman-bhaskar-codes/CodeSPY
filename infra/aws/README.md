# AWS OIDC bootstrap proposal

Region: ap-south-1 (Mumbai), selected by owner.

Proposed account changes: add the GitHub OIDC provider with sts.amazonaws.com audience; create /codespy/CodeSPYGitHubValidation using the exact main-branch trust in github-oidc-trust.json; attach github-validation-policy.json. Replace ACCOUNT_ID with the authenticated account. The role can validate identity only; it cannot deploy resources, read project data or manage IAM.

Status: created and read back after explicit owner approval. The GitHub provider, role and IdentityValidationOnly inline policy exist. AWS_VALIDATION_ROLE_ARN is stored in GitHub Actions secrets; Mumbai is the repository region. The manual AWS identity validation workflow tests the OIDC path without deploying resources.

Verified account findings: root MFA enabled, no root access keys, no existing GitHub OIDC provider, no custom IAM users; existing USD 5 monthly account budget named Portfolio Website. Existing resources and budget were left untouched. No CloudTrail trails were returned in Mumbai; this does not establish absence of event history or other regions' trails. Credits/expiry and billing contacts were not verified.
