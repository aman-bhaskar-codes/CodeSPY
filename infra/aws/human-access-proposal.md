# Human access bootstrap

The account is standalone, with no AWS Organizations organization and no Identity Center instance in the checked regions. An Identity Center account instance is not a substitute for organization-level AWS account access.

Runbook fallback proposal: IAM group codespy-admins and user codespy-aman, with AdministratorAccess for initial bootstrap and an explicit deny on privileged operations until MFA is present. No access keys will be created. The owner must enter their own console password and enroll MFA; the agent will not handle a password. CLI login will then use a short-lived named codespy profile.

This is a persistent account-security change. Record actual approval/mutation results before considering the human login ready. The GitHub validation role remains identity-only and does not gain deployment permissions.
