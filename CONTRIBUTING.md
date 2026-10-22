# Contributing

Use `feature/<capability>` or `fix/<defect>` branches from the appropriate reviewed base. Keep one logical change per commit and open a pull request. Merge through required checks, then delete the delivery branch.

Commit subjects use `<type>(<scope>): <imperative description>` and stay under 72 characters. Supported types: feat, fix, refactor, perf, test, docs, chore. For example: `fix(parser): preserve UTF-8 source ranges`.

Describe code behavior in commits, pull requests, README, comments and docstrings. Keep execution schedules and owner notes local. Do not use internal delivery labels or placeholder subjects. Do not rewrite published history or force-push without explicit authorization.

Commit lockfiles with dependency changes. Add meaningful behavior, contract and integration checks for affected code. Preserve historical database revisions and public compatibility. Never commit secrets or private analyzed artifacts. External account changes and deployment require explicit owner authorization.
