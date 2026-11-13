# Report narrative

A model-written summary of one stored architecture report, built as a synthetic investigation (mode
`report_narrative`, target `architecture_report`). Every sentence cites report statements by id and is checked by
code against what the investigation was actually shown. [ADR 0007](../adr/0007-report-narrative.md) records the
decisions. Everything below is proven offline; **no live model call has been made**. The live check is owner-run.

## Implemented boundary

- **Pinned.** A spec carries `architecture_report_id` and `architecture_report_digest` (both or neither; required
  exactly for the narrative mode and target). Both are left out of the hashed spec when `None`, so every existing spec
  hash is unchanged (a literal pre-change hash is pinned in a test). The API request carries only the report id; the
  server reads the digest from the stored report within the same organization, repository and snapshot.
- **Tool.** `read_report_section(section, offset)` serves whole statements `{sid, kind, code, text, params}` in pages
  of at most 40 statements or 16 KiB. It is never advertised to other modes, refuses without a pin, across tenants,
  repositories or snapshots, and when the stored report's digest is not the pinned one. String parameters pass
  through the model-safe transform. Every statement served writes one immutable `report_fact` evidence row
  (`fact_ref` = report digest and statement id) in a table whose CHECK makes each row fully source-bound or fully
  report-bound.
- **Statement ids.** `sha256({section, code, params})[:16]` plus an occurrence index for identical statements of one
  section; computed, not stored, so the report digest is unchanged.
- **Executor.** GATHER, then one NARRATE node per page (read the page through the tool, one model call with tools
  off), then a deterministic VERIFY and FINALIZE. All effects are journaled under ids derived from the node id, so a
  restart replays instead of paying again. Budget, step and context stops end with a partial narrative that lists
  the sections not narrated. The model task is `synthesizer`; the closed task list is unchanged.
- **Prompts.** `backend/app/agents/prompts/report_narrative/v1/` (system prompt, section template, output schema).
  The version is pinned by a digest test; a change needs a new version directory. Instructions are static; report
  data sits between marker lines derived from ids the run created (a page that contains its own marker is refused).
  The spec records the prompt revision. A reply is parsed strictly (fence strip, JSON object, schema); a failed
  page is counted and left out, with no automatic second call.
- **Verifier.** A sentence is confirmed only if every cited statement was delivered to this investigation as a
  `report_fact` receipt of the pinned report and the delivered page still hashes to its receipt; backticked names and
  numbers in the sentence appear in the cited statements; and an `unknown` statement is supported only by a sentence
  that says unknown or restates it. Source evidence never supports a narrative sentence. Unconfirmed sentences are
  shown as unverified, never hidden. The narrative is grounded at 0.8 confirmed or more; the verdict is always
  `indeterminate`.
- **Publication.** The finalizer writes the report and, for a narrative draft, an immutable
  `architecture_report_narratives` row with the structured sentences; a database guard requires the spec to pin that
  report and digest, the same repository and snapshot, and the published investigation report.
- **Read and viewer.** `GET .../architecture-reports/{id}/narrative` returns the latest narrative with the cited
  statements and their evidence locations (404 when none). Creation uses the existing
  `POST /investigations` and `/execute` routes (so `inference_unavailable` 503 applies). The workbench shows each
  sentence with Observed, Derived, Inferred, Unknown or Unverified, expandable cited statements and the sections that
  were not narrated.

## Owner-run live check (not run by this project's tests or agents)

1. Run the offline half first: `python scripts/narrative_smoke.py --help`; the script refuses without
   `CODESPY_LIVE_BEDROCK=1` and `AWS_PROFILE`.
2. With a profile you control and a qualified model or inference profile in `ap-south-1`:
   `CODESPY_LIVE_BEDROCK=1 AWS_PROFILE=<p> uv run --project backend python scripts/narrative_smoke.py
   --converse-model <id> --demo --max-pages 3`. It makes at most `--max-pages` calls of at most 1,500 output tokens and
   prints per-page statements, sentences, confirmed count, tokens and latency, then the verified ratio and the prompt
   revision. Run it on a real report with `--report-file <report.json>` (the JSON of the report GET).
3. Pass criteria: no parse failures, verified ratio 0.8 or more, and no confirmed sentence that overstates its
   statements. A failing run is a prompt change, which means a new version directory, not an edit.
4. To run the product path you must compose, and this project does not: a worker entrypoint calling
   `build_worker_runtime`, a 32-byte continuation key, a funded repository budget (`ModelBudgetService.ensure_budget`),
   an installed generation price schedule, a repository disclosure policy allowing the provider and region, a
   qualified model profile with its task route for `synthesizer`, and a database role that can read
   `architecture_reports` and `architecture_report_narratives` (migration 0049 grants the test worker role only).

## Limitations

- No real model has produced a narrative here; quality is judged only by the owner-run check. Offline tests prove
  structure, safety, accounting and replay, not prose.
- The deterministic check is conservative: it can mark a valid paraphrase unverified (for example a number written as
  a word), and it only checks names the model wrote in backticks and digits. It does not judge that a sentence is
  complete or relevant.
- The full executor on PostgreSQL with the encrypted SQL journal and real fencing is not exercised end to end. The
  executor is tested in memory with a scripted model, the tool, migration, guard, read route and finalizer insertion
  are tested on PostgreSQL or SQLite separately.
- At most 50 sentences are kept, at most 200 evidence references are listed in the report, and a run covers 8
  sections at 40 statements per page within 25 steps.
- Delimiters reduce harm from hostile report text but are not a defence by themselves; the real controls are: no
  tools for the model, schema-checked output, and verification by code. Whether Bedrock guardrails evaluate
  Converse content is unconfirmed and nothing here depends on it.
- A narrative started through the existing investigation routes uses the default budget (5,000,000 micros).
