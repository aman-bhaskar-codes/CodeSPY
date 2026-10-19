# Trusted semantic sidecar

Build using pinned Node 22 and TypeScript 5.9.3. The fixed entry is
`dist/src/cli.js`; stdin and stdout carry the version 1 JSON protocol. No target
scripts or dependencies are loaded. Run it inside the qualified analyzer
container, which supplies native memory, wall, cancellation and file-output
limits. The Python adapter invokes this fixed entry only.

Compiler positions are UTF16 offsets into original, unnormalized source. The
control plane converts these through its SourceIndex before publication.
Candidate files always belong to the admitted source map. Trusted packaged
standard library symbols can explain unavailable declarations but do not become
file-backed graph nodes.

Each admitted configuration gets an independent compiler program, except configs
used solely as extends bases. Config `files`, `include` and `exclude` select roots;
imports may bring other admitted files into that program. Local bounded extends
inherits the base context; project references create independent contexts. A source
shared across contexts retains the union of declaration candidates; conflicting
bindings are ambiguous. An unavailable context or exhausted project-site budget
cannot supply a confident target. Config errors are explicit diagnostics.

The supported selection glob language is `*`, `**`, `?` and directory prefixes.
All selections and resolution remain restricted to admitted virtual maps. Plugins,
external extends, parent traversal and external path substitutions are forbidden.

Normative binding examples:

```ts
function target() {}
function invoke(cb: () => void) { cb(); }
invoke(target);
```

The `cb` use binds lexically to parameter `cb`. The `cb()` call target is unresolved:
the argument at one invocation does not establish its runtime target. `invoke()`
can identify the declared `invoke` function. Similarly `const alias = target;
alias()` binds `alias` lexically but abstains on a runtime call target. A direct
import of a uniquely declared function can supply a callable definition candidate;
type-only imports, unknown receivers and method calls abstain. Overload declaration
multiplicity is retained as multiple candidates, not arbitrarily selected.
