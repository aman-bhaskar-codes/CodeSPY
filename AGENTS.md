# CodeSPY repository instructions

Describe implemented capabilities and limitations directly. Keep execution documents, owner notes and delivery schedules local and excluded from version control. Optional local owner instructions are in `.codex/USER_PREFERENCES.md`; direct user instructions take precedence.

Use scoped Conventional Commits: `<type>(<scope>): <imperative description>`, under 72 characters. Types: feat, fix, refactor, perf, test, docs, chore. Name branches `feature/<capability>` or `fix/<defect>`. Keep commits focused, use reviewed pull requests, and delete delivery branches after merge. Never rewrite published history or force-push without explicit authorization.

Public documentation, UI copy, comments, docstrings and delivery metadata use capability names rather than internal sequencing labels. Preserve database revisions, public contracts and ownership identifiers; a prose cleanup must not change their semantics.

The owner fills provider/API secrets. Ask before sensitive credential access, AWS account/resource changes, paid calls, deployment or destructive operations. Use only owned disposable resources for integration checks, preserve developer files/volumes and clean up owned resources. Never execute uploaded source or install uploaded dependencies.
