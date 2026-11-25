# ADR 0008: Agent runtime and execution policy

Status: accepted. Nothing in this record needs a live model or a cloud account; the first implementation runs against
a fake model and a local sandbox. Builds on [ADR 0005](0005-bedrock-runtime.md) and [ADR 0007](0007-report-narrative.md);
sandbox tiers are in [ADR 0009](0009-execution-sandbox-tiers.md).

## Context

CodeSPY analyses repositories without ever running them. A coding agent has to edit files and run builds and tests, which
is running uploaded source and installing its dependencies. The repository rule that forbade this ("Never execute uploaded
source or install uploaded dependencies") was correct for an analysis product and cannot stand for an agentic one.

## Decisions

1. **Execution rule.** Uploaded source runs only inside an execution sandbox tier. It never runs in the API, worker or
   analyzer processes, and only inside an agent session started by an authorized member for that repository. The
   analyzers, admission and scanners keep their no-execution guarantee unchanged. `AGENTS.md` carries this rule.
2. **One agent loop per session.** One primary loop (model call, tool calls, observations, repeat). Read-only explorer
   subagents may be added later for verbose exploration. Review is a deterministic gate (tests, lint, verifier), with an
   optional model review only on request. A planner/worker/reviewer team is not the default.
3. **Reuse the investigation primitives, not its graph.** A new session runner reuses the job lease and authority port,
   the encrypted effect journal, the model gateway, budgets, tool contracts, the prompt-asset pattern and the verifier.
   The fixed investigation node graph and its snapshot-only scope stay unchanged; investigations become a callable
   read-only tool. Extending the investigation node kinds with edit and exec nodes is rejected because it would break
   replay semantics, spec hashes and the immutable tables.
4. **Replay with side effects: intent, then effect, then receipt.** Model calls and read tools replay from the journal.
   Edits are content-addressed (pre-image hash to post-image hash), so re-applying an edit whose post-image is already
   present is a no-op. Commands are never re-run automatically: an intent without a receipt becomes
   `outcome_unknown`, and both the model and the user are told.
5. **Patch format.** Structured SEARCH/REPLACE edits, each carrying the file's expected hash, plus create, delete and
   move tools. Paths are normalised with the archive path rules: no `..`, no absolute paths, no symlink traversal.
   Unified diffs from the model and whole-file rewrites are rejected as default formats.
6. **Workspace.** The workspace is server-authoritative, materialised from an immutable snapshot into a Git working tree
   inside the sandbox. Revisions are content-addressed change sets in the database, blobs live in the object store, and
   checkpoints are shadow-ref commits mirrored as bundles so undo survives sandbox loss. Snapshots stay immutable.
7. **User and agent edits.** Optimistic concurrency per file: every write names its base hash. Disjoint files never
   conflict; a same-file conflict is a 409 to the browser and a conflict event to the agent, which must re-read. No
   collaborative-editing algorithm until multi-user editing exists.
8. **Permissions.** Tool classes are read, edit, exec, git, network, mcp and re. The effective policy is the org policy
   intersected with the session settings, and deny wins. Rules are prefix patterns. The default posture, chosen by the
   owner:
   - reads run automatically;
   - every patch needs approval;
   - every command needs approval, with an "always allow this command in this session" rule available;
   - publishing to Git always needs approval and can never be auto-approved;
   - network and MCP tools are off.

   An org may allow auto-applied edits because checkpoints make them reversible. A built-in denylist (for example
   recursive deletes of the root) applies even to auto-approved commands.

9. **Transport.** Server-Sent Events for session events and HTTPS POST for commands, with persisted events and
   cross-process fan-out through PostgreSQL notifications. The contract is specified in the agent event protocol.
   A single WebSocket for everything is rejected: it needs its own reconnect, routing and auth story.
10. **Model routing and disclosure.** The main loop uses the configured agent profile; compaction, explorer subagents
    and titles use a fast profile; a deep profile is used only when the user picks it. All calls go through the
    existing gateway, budget and disclosure policy, and there is never a silent switch of provider. For the beta the
    owner accepts Bedrock global inference profiles, which can process prompts outside India. This is disclosed in the
    product, and a regional profile replaces it when one exists for the chosen models.
11. **Limits (owner-tunable defaults).** USD 5 per session, USD 50 per org per day, 150 tool calls per session,
    60 steps per turn, an alert at 80% of any cap. Reservations are made before every model call, as they are today.
12. **Production composition.** Two entrypoints (API and worker) with one settings object, and an explicit opt-in for
    the live model runtime. Composing inside the existing application module is rejected.
13. **Kill switch.** A global flag and a per-org flag are checked on every step; cancelling a session releases its
    sandbox.

## Consequences

- Every phase that runs code is blocked on the sandbox port existing; until then the agent can read and propose only.
- Threats are bounded by capability rather than by prompt wording: no egress, approvals for exec and publish, per-org
  scoping on every table, and an append-only audit log. Delimiters and heuristics are a harm reducer, not the defence.
- Cost control relies on the existing reservation mechanism plus per-session and per-org caps.

## Changes this record makes outside code

- `AGENTS.md`: the execution rule is replaced by decision 1.

## Not decided here

Identity provider beyond GitHub App sign-in, package installs from the sandbox, publishing to GitHub, retention periods
and telemetry are separate owner decisions taken when those capabilities are built.
