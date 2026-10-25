# Index interfaces and workbench controls

The local implementation exposes scoped index creation, availability, job status,
cancellation and retry. The server supplies the qualified configuration and runtime;
public start requests accept only a request key. Application wiring remains disabled
unless configuration and a builder are injected. Request identities, route scope,
CSRF and current write authority are checked by the existing services.

The workbench supports manual builds, status polling, cancellation confirmation,
authorized retry, coverage and persisted-set provenance. Retrieval transport aliases
use generated API definitions. Search and context requests carry the selected set
pin. The read-only MCP status tool uses the same job service and status contract;
it does not provide index mutation tools.

## Verification checkpoint

25 October 2026; reference 83767b105f0eb0870c79f84402241d729084120b plus local changes:

- Final affected backend selection: **949 passed** in 29.67 seconds, including
  retrieval, investigation, structural and ingestion unit/transport checks.
- Frontend: **32 tests passed** across 10 files; typing, lint and production build passed.
- Strict backend typing passed for **225 application files**; Ruff passed after
  correcting two assertion-style lint findings in the new regression.
- OpenAPI export and TypeScript generation succeeded and exactly matched checkout outputs.
- **Four real PostgreSQL/browser checks passed** in 25.01 seconds: HTTP lifecycle,
  authorization/CSRF/configuration rejection, principal-bound retries, API/MCP
  parity and the admitted-source/index/search/context/reload/cancel browser flow.
- The fresh Playwright browser journey passed in 17.1 seconds. It uses real local
  API, database, worker and isolated reader/indexer containers. Archive admission
  is a trusted fixture, not an additional native admission qualification. No live
  embedding/model service was used.

The first browser run exposed a status-read race: lifecycle and publication fields
were read in separate statements, allowing a commit between them to produce an
invalid DTO. The index projection now refreshes all fields from one statement
snapshot. A regression failed before the fix, passed afterward, and the complete
browser journey then passed. No database authority or resource limit was relaxed.

A broad unconfigured backend attempt previously reported 76 database environment
setup errors and two stale SQLite transport failures. The two transport fixtures
now explicitly simulate source/selection authority and assert current unbound-memory
and unconfigured-provider behavior. The final 949-test selection passed; this is
not a claim that the older standalone PostgreSQL suites ran with their separate
required environment. Whole-system qualification and independent final review
remain separate work.

Changes remain local and uncommitted. No AWS access, real credentials, provider
calls or deployment were performed. Default index runtime wiring remains disabled
unless explicitly configured. Owned test services are removed at handoff; trusted
images and the downloaded test browser may remain idle.
