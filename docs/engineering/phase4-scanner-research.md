# Scanner adapter qualification notes

These observations prepare P4.02. They do not enable a scanner or establish Linux hostile-input isolation.

## Inspected references

- [Gitleaks v8.30.1 report fields](https://github.com/gitleaks/gitleaks/blob/v8.30.1/report/finding.go): reports include content-bearing fields and fingerprints. Do not retain raw reports or forward them to broker/public logs. Redaction of a secret field is not proof that arbitrary report fields are safe.
- [Gitleaks v8.30.1 location implementation](https://github.com/gitleaks/gitleaks/blob/v8.30.1/detect/location.go): reported columns need a pinned conversion and actual-byte validation. Do not apply a generic line/column convention.
- [Gitleaks v8.30.1 file walker](https://github.com/gitleaks/gitleaks/blob/v8.30.1/sources/files.go): skipped/unreadable paths must not be inferred processed from exit code alone. Stage only authenticated generated aliases; reject symlinks; qualify readable/empty/oversized/error behavior. A bounded per-file stdin mode is an alternative requiring its own fragment/range tests.
- [Semgrep CLI exit codes](https://docs.semgrep.dev/cli-reference/#exit-codes): behavior depends on explicit error/strict flags. Pin the command profile and validate report coverage/errors as well as process status. Empty results alone never certify a completed scan.

No upstream scanner implementation was copied. Use CodeSPY-authored Semgrep rules; separately review any attributed Gitleaks rules and retain notices.

## Actual local Gitleaks probe

REA independently inspected the installed Darwin arm64 artifact without execution: evidence `ev_aac38ce7d1068f3b571cc273b76e76a81f99083d20b4c4c7a86de0bc26447447`, SHA-256 `f414bc2fb952be6c9072b75cb411e3368614ef4b16d48dbd9ad238034afd2302`. Its embedded module version is `(devel)`; executing its bounded `version` command separately returned `8.30.1`.

A subsequent owned synthetic probe executed that trusted scanner with an authored canary rule, an empty inherited credential environment, decoding/archive traversal disabled, explicit full redaction and a five-second tool timeout. No owner repository/credential was scanned. Temporary files were removed. Returned exit code was 1, four findings, with redacted Secret fields. Safe positional observations:

| Fixture | Reported start/end line | Reported start/end column | Expected half-open byte range |
|---|---|---|---|
| ASCII prefix | 1/1 | 8/34 | [7,34) |
| UTF-8 prefix + CRLF | 1/1 | 7/33 | [6,33) |
| Second CRLF line | 2/2 | 2/28 | [7,34) |
| Final line without newline | 2/2 | 2/28 | [6,33) |

For the observed later-line cases, columns are relative to the preceding newline byte, rather than the expected first character after that newline. Blindly subtracting one from every reported column would produce wrong source citations. These four probes do not establish behavior across fragment boundaries, multiline matches, decoding, archives or all platform builds. Qualification must include those boundaries or explicitly reject unsupported cases before publishing evidence.

Next P4.02 proof includes actual pinned Linux output, authored detector cases, malformed/flooded diagnostics, cancellation/cleanup and zero secret sinks. Native source scopes, toolchain/rule digests and successful processed coverage must be validated before constructing the public metadata-only result.
