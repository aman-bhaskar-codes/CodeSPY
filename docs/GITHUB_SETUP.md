# GitHub setup

Public repository: https://github.com/aman-bhaskar-codes/CodeSPY

Local main branch uses Aman Bhaskar with the verified GitHub numeric-ID noreply address. The global Fleet Bot configuration is unchanged.

Secret scanning and push protection were reported enabled by GitHub; Dependabot alerts and security updates were enabled. Dependabot configuration covers uv, npm and Actions. Setup workflow checks frozen dependency installation, config hygiene and documentation formatting. No application tests are claimed.

Before launch: choose a license, configure private vulnerability reporting, review branch protection/rulesets and required status checks after the first successful CI run, and configure protected deployment environments before adding deployment permissions; identity-only AWS OIDC was configured and verified during setup.

Main branch protection requires the dependencies check and an up-to-date branch, disallows force pushes and deletion, and requires linear history. Administrator enforcement is disabled so the project owner retains recovery access.
