# Phase 3 REA reference investigation

Date: 19 October 2026. CodeSPY baseline `736a5ef831ed4a81899916153a7d09a13a51f2d1`. This is an engineering investigation, not a product REA adapter or a completed structural-analysis feature.

## Actual connected tool

`binary_session` reported installed `rea-agents` 6.7.0, 207 MCP tools, catalog SHA256 `96d8f6e6ea1b1ba13557216624a3461c738729d53c79b4b0100d1f3a3a32441c`. Build commit was null; do not equate this published package with a particular upstream source commit. The historical blueprint's 6.5.0 source pin remains historical evidence.

Static JavaScript application analysis was available. Native Ghidra was not configured in the connected server; no native engine was needed or installed. No configuration repair, browser attachment, runtime target execution or provider credential was needed.

## Recorded operation and scope

Operation: `analyze_javascript_application`, directory format, summary view. Target: the installed 6.7.0 package's `dist/domain` subtree. Exact selected files: `evidence.js` and `relationshipCoverage.js`. Artifact subtree SHA256: `59c84185322c85950027b0ceb18eb076773b09784a0c54a75661130ea0a59a29`.

Parent evidence: `ev_fef147955538e680b87c3fe88ea74af25cdbb8bb87b99204ba744305f8435796`.
Summary evidence: `ev_c519fa003aa5363a9b490a85cf84a317140125b1137d623b977b882deceb036a`.

The tool read 14,648 text bytes, parsed two files, visited 1,697 AST nodes and reported zero parse failures. It left 419 inventoried files unselected. Its semantic unknown retention reached the requested 1,000-record bound; application and semantic coverage were partial. These counts are not vulnerability findings or evidence of complete application behavior.

A portable evidence bundle was exported to ignored `work/phase3/rea-reference-evidence.json`. It contains two records, about 5.2 MB. Evidence IDs are content references; the MCP server's retained views require that same live session, while the exported file supports later offline inspection. It is deliberately excluded from the source commit.

## Source-corroborated techniques

| Observed mechanism                                                      | Source corroboration                                            | CodeSPY decision                                                                                          |
| ----------------------------------------------------------------------- | --------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| Strict provider identity and separate observed/derived/inferred basis   | Installed `dist/domain/evidence.js`                             | Preserve existing evidence contracts; later structural records declare their basis                        |
| Complete/partial/unknown relationship states with reasons               | Installed `dist/domain/relationshipCoverage.js`                 | Keep structural coverage explicit; no conversion of missing coverage into complete                        |
| Provider/profile identity must agree                                    | Installed `validateAnalysisProfileProvider`                     | Bind future analyzer output to its actual toolchain/profile                                               |
| Semantic evidence IDs validated against content                         | Installed `authenticatedEvidenceSchema` and `computeEvidenceId` | Retain CodeSPY's own canonicalization and scoped identity rules; do not copy incompatible upstream hashes |
| Limited analysis preserves unknowns instead of inventing a closed graph | Returned REA summary and unknown cap                            | Treat output/candidate budgets as visible partial results                                                 |

This is conceptual adaptation, not copied upstream implementation. The installed REA license is MIT; no REA runtime dependency or production sidecar was added.

## Focused primary references

- [REA MCP contracts](https://github.com/morluto/rea/blob/main/docs/mcp-contracts.md): live server identity is authoritative; rolling docs can differ from the installed release.
- [py-tree-sitter 0.26.0 release](https://github.com/tree-sitter/py-tree-sitter/releases/tag/v0.26.0), [pinned parser bridge](https://github.com/tree-sitter/py-tree-sitter/blob/v0.26.0/tree_sitter/binding/parser.c): removed timeout APIs and actual callback behavior. Local probes, not release wording alone, determined supported use.
- [TypeScript compiler API](https://github.com/microsoft/TypeScript/wiki/Using-the-Compiler-API): explicit compiler host separates source acquisition from checker behavior. The selected 5.9.3 package was tested locally; a production admitted-file virtual host remains later work.

## Limits and nonclaims

REA parsed inert shipped JavaScript and did not execute its target files. Source inspection corroborated schema mechanisms; static candidate relationships are not execution observations. This was not a full REA audit, parser security review or comparative quality benchmark. No external repository was modified, and no arbitrary repository/package scripts were run. Rather than importing many tools, Phase 3 retains only the plan's justified parser/compiler choices and existing infrastructure.
