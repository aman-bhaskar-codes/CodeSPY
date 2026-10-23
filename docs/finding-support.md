# Finding detail and current support

Finding detail keeps `historical_disposition` separate from `disposition`. A historical `supported-static` decision is returned as current support only when the retained scan passes the existing durable job, attempt, run and report association checks, availability of admitted source-object records and current repository authorization. This reuses the existing database availability contract; it does not add an object-store availability probe.

Dependency findings additionally require the retained advisory bundle and its exact current answer. An unavailable bundle, withdrawn or stale answer, or unavailable source downgrades the effective disposition to `unresolved`. This does not mutate historical verification or triage. Missing or inconsistent retained support is not treated as current support. An unresolved historical decision is never promoted by reading it.

The detail endpoint does not open uploaded archives. `code_excerpt` is currently `null`, including when an object store is configured. Excerpt display requires a separately qualified isolated source reader with exact file identity, range checks and whole-file privacy transformation. The former in-process ZIP fallback has been removed. Source paths and line metadata remain available; no claim of safe excerpt qualification is made.

List and summary disposition counts still describe historical decisions; this detail projection does not change their filtering or aggregation semantics. Current-support aggregation and isolated excerpt retrieval remain separate integration work. No live advisory queries, provider calls or uploaded-code execution happen on a detail read.
