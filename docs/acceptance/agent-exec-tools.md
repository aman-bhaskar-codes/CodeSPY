# Acceptance: agent exec and test tools

Scope: `run_command` and `run_tests`, which run commands and test suites in the sandbox and report exactly what happened,
with live output coalesced for the stream and file changes recorded in the workspace. Policy is in
[ADR 0008](../adr/0008-agent-runtime-and-execution-policy.md); approval comes with the agent loop.

## What exists

| Part                                                                                  | Where                                                   |
| ------------------------------------------------------------------------------------- | ------------------------------------------------------- |
| The two tools, command planning, refusal and risk                                     | `backend/app/services/agent/tools/exec_tools.py`        |
| Runner detection, command templates, report parsers (pytest, vitest, jest, go, cargo) | `backend/app/services/agent/tools/runners.py`           |
| Output coalescing for `tool.call.output`                                              | `backend/app/services/agent/tools/output.py`            |
| Recording a command's file changes (`sync`)                                           | `backend/app/services/workspace/{local,service}.py`     |
| Recorded runner reports and the tests                                                 | `backend/tests/agent/tools/fixtures/`, `test_exec_*.py` |

## Reported exactly

- **Exit codes.** The first line of the result after the command is `exit code N in X.X s`, and `data.exit_code` holds it. A
  failing command is a successful tool call: status `ok`, never an error, so the model reads the code and the output itself.
  Codes 0, 1, 2, 3, 7, 126, 127, 137 and 255 are tested offline, and a selection on real containers.
- **Timeouts.** Status `timed_out`, no exit code, and the text says how long the limit was
  (`timed out after 2 s ...`). After a real timeout the sandbox still serves the next command.
- **Output limits.** The sandbox keeps at most 4 MiB; the result says the output was cut, `data.output_bytes` is exactly the
  bytes kept (4,194,304 on a real 5 MiB run), and the model sees at most 32 KiB with the start and the end kept. A command stopped
  for filling the disk is reported as such, apart from a timeout.
- **Streaming.** Output goes to the stream in coalesced batches (1 KiB, 200 ms, or a switch between stdout and stderr), each with
  its UTF-16 offset in its stream; live output stops at 256 KiB with a notice, and the report that follows a test run is not
  streamed.

## Tests

`run_tests` detects the runner (pytest, vitest, jest, go, cargo; python first when several are present) or takes one by name,
runs it with a fixed command template, and returns counts and up to 50 failures with name, file, line and message in a
structured `tests` field. The runner emits `test.result` from it (`event_payload`); the payload is valid under the protocol
for the recorded pytest and vitest reports. A report that cannot be read yields no summary and the raw output, not a guess.
XML with a DOCTYPE or entities and oversized reports are refused before parsing.

## Safety

- Commands run in the sandbox only, as an argv with no shell. `shell` is a separate argument for pipes and redirects, always
  medium or high risk, and is how an approval rule can treat it differently.
- Never run, whatever the approval policy: deleting the root, the workspace or system directories recursively, filesystem
  formatting, writing to a device, privilege escalation, shutdown and fork bombs.
- A test selector or keyword cannot start with `-` and is passed as an argument, never spliced into the script.
- Environment variables that change how programs load (`LD_*`, `DYLD_*`, shell start-up) are refused; the working directory must
  be inside the workspace; the timeout cannot exceed the sandbox's.
- Commands, output and failure text are redacted, including the secrets inside the structured test summary.
- `risk` is a signal for the approval card, not the gate.

## Files a command changes

After every command, files that differ from the last recorded state become one workspace revision with a `workspace.revision`
event, and the result names them. Reading commands record nothing. Edits by the edit tools and by commands do not double-count.

## Verification

```
cd backend
uv run --frozen pytest -c pyproject.toml tests/agent/tools -q -o addopts=
CODESPY_SANDBOX_IMAGE=codespy-sandbox:dev CODESPY_TEST_SCHEMA_TARGET=head uv run --frozen pytest -c pyproject.toml tests/agent -q -o addopts=
```

437 agent tests pass with Docker and PostgreSQL (323 without them). New here: 78 runner and command tests, 17 for `run_tests`,
16 on real containers (including a stand-in pytest that writes a real JUnit report inside the container) and 2 on PostgreSQL.

## Limits

- The sandbox image holds Python only, so real vitest, jest, go and cargo runs are covered by recorded reports and scripted
  runs, not live; the image gains those tools when a project needs them.
- Package installs need network and are off; a command that needs a download fails with its own error.
- Risk and the refusal list are heuristics over the command text, not a parser of every shell construct.
