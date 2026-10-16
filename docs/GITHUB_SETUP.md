# GitHub setup

Public repository: https://github.com/aman-bhaskar-codes/CodeSPY

- MIT LICENSE, security policy, contribution guide, bug/feature templates and project README.
- Local main branch uses Aman Bhaskar with the verified GitHub numeric-ID noreply address; global Git identity is unchanged.
- Secret scanning, push protection, Dependabot alerts/security updates and private vulnerability reporting were enabled during repository setup.
- Main branch requires the dependencies check and linear history, disallows force pushes/deletion, and retains owner-administrator recovery access.
- Setup CI installs locked backend/frontend packages, runs hooks, configuration lint/type checks and local Docker dependency integration. It never configures API keys or signs in to AWS.
- CodeSPY-Dev OAuth client registered with homepage http://localhost:5173 and callback http://localhost:8000/api/v1/auth/github/callback. Wildcard redirects and device flow are off. No client secret or PAT was generated.
- Any future OAuth/client/token configuration is performed manually by the owner, as instructed.

Application tests and build claims are intentionally absent until application sources exist. The separate earlier manual AWS identity workflow must not be triggered by the assistant under the owner's account-access restriction.
