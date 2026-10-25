# Finding support and source previews

Finding detail, lists and scan summaries use one current-support evaluator.
`disposition` and `by_disposition` describe effective support at that read;
`historical_disposition` and `by_historical_disposition` retain the immutable
published decision. Reading cannot promote an unresolved historical decision.

A historical `supported-static` decision requires the retained scan's durable
job, attempt, run and report associations, available admitted-source object
records and current repository authority. Dependency findings also require the
retained advisory bundle and its current exact answer. Missing, withdrawn,
stale or inconsistent support becomes `unresolved`, without changing historical
verification or triage. These availability checks use the existing database
object-state contract, not a physical object-store probe on every list query.

Disposition filters apply to this same effective projection in SQL **before**
matching counts, page limits and UUID keyset pagination. Each request evaluates
its own current support; counts/membership can change between live pages when
source or advisory support changes. Stable cursors do not freeze support.

## Redacted finding spans

With a qualified `SourceReaderPort` configured, currently supported source-pattern
findings may return `code_excerpt` and `excerpt_reason=available`. The source
view verifies the complete admitted file inside the offline reader, transforms
the whole file using the existing model-safe privacy policy, then maps the
original finding offsets through its redaction intervals. Original byte offsets
are never used as transformed offsets. Previews are at most 8 KiB; unmappable,
truncated, invalid UTF-8 or oversized spans are omitted rather than guessed.

Secret-format findings never expose a preview, including their matched values.
Dependency findings have no source-span preview. Missing runtime/source,
privacy exclusions and unmappable ranges return null with an explicit reason.
Authorization revocation fails the read; it is not disguised as missing preview.

The UI distinguishes current support from history and renders source as text.
Heuristic redaction can miss secrets; preview responses retain the privacy
limitations. A preview is source context, not additional proof of a finding.

Derived privacy records use conflict-safe registration and exact metadata
verification. Identical concurrent reads share one ready identity. Failed,
tombstoned, expired or inconsistent identities are not silently resurrected.
No advisory network call, model call or uploaded-code execution occurs here.
