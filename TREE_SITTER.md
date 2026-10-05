# Tree-sitter: design notes

> **Status:** shipped. tree-sitter is a pinned runtime dependency
> (`pyproject.toml`, `requirements.lock`); the deterministic structural
> layer lives in `semantic_code_review/structural/` (`parse.py`,
> `symbols.py`, `diff.py`). This doc is the design history — why the dep
> is carried and what remains unbuilt. The live vocabulary (Symbol,
> SymbolDelta, Symbols axis, overview seed) is defined in
> [`CONTEXT.md`](CONTEXT.md).

## Why the dep is carried

tree-sitter was skipped while the viewer was a static, self-contained
HTML artifact: every structural pre-computation had to be paid up front
for files the reviewer might never open, and it added a native-binding
Python dep against the supply-chain pinning regime we'd just tightened.

The calculus changed once `scr review` became a long-running localhost
HTTP server with a back-channel:

- **Lazy parsing.** A file's AST is built only when the reviewer expands
  it — up-front parsing of every touched file is no longer the only option.
- **Cached state across calls.** The server holds parsed ASTs in memory
  across every per-hunk call, fold-toggle, and ref-click. Parse once,
  query many.
- **Interactive features that need ground truth.** Click-to-jump, symbol
  cross-referencing, and "where else is this used" become affordable when
  the parse is already resident.

## Shipped

- **Symbol grouping (the Symbols axis).** The sidebar carries a
  deterministic structural axis alongside LLM-curated themes: top-level
  symbols enumerated from each touched file's AST, each linking to the
  hunks that touch it (`viewer/build_json.py` `_symbol_blocks`,
  `structural/symbols.py`). Themes answer "what is this PR for?"; symbols
  answer "show me everything about X".
- **AST fold regions.** Fold ranges come from real function / class /
  block boundaries, not indentation (`viewer/fold_regions.py`), over the
  whole file on both sides. The indent heuristic remains only as the
  fallback for languages without a shipped grammar, and for column-0
  stanzas no definition covers.
- **Deterministic overview seed.** The overview pass is seeded with the
  base→head `SymbolDelta` (`augment/overview.py` `_format_symbol_seed`),
  so the symbol inventory comes from the parse rather than the LLM
  guessing — and the model no longer restates it, which is what its
  `symbols_*` fields were (ADR 0001, amended).

Grammars ship as four individual packages — `tree-sitter-python`,
`tree-sitter-javascript`, `tree-sitter-typescript`, `tree-sitter-rust`.
Other languages degrade to the indent-based fold fallback: the floor is
unchanged, the ceiling rises only for what we ship.

### Rust

`.rs` files use `tree-sitter-rust==0.24.2` with a curated tag query in
`structural/parse.py`. Definitions cover functions (including methods and
bodyless declarations), structs, enums, unions, traits, type aliases,
associated types, constants, statics, modules, and `macro_rules!` macros.
Each source definition is captured once. Fields, variants, parameters,
local bindings, and imports are not separate symbols.

Signatures retain declared headers, modifiers, generics, lifetimes, and
`where` clauses, excluding bodies and constant/static initializers. Tuple
structs retain their fields; type aliases retain their target; macro
signatures stop at the name. Reference queries cover direct, qualified,
method, generic, and macro calls, plus implementation-header trait/type
names. References remain name-based. Macros are not expanded and
conditional compilation is not evaluated.

Nesting follows captured source containment: modules and traits contain
their declarations. `impl` blocks are not symbols, so their methods and
associated items appear under the nearest captured enclosing definition,
or at the root; they are not attached to a separately declared struct.
Repeated names can collide in the change detector's qualified-name map.
The follow-up is [Rust symbol identity across impl blocks](PARKED_IDEAS.md#rust-symbol-identity-across-impl-blocks).

## Not yet built

**Semantic hunk splitting.** Hunks are still whatever `git diff`
coalesced; the per-hunk pass does some semantic splitting after the fact
via the optional `segments[]` field. Moving boundary detection upstream —
re-segmenting the diff so each segment maps to one changed AST node —
would make it deterministic and retire the prompt's "split into segments"
guidance. It reshapes the per-hunk pipeline, so it is the most ambitious
remaining step.

Two stretch goals fall out once AST diffs drive segmentation:

- **Cross-file move detection.** Same body-hash AST node deleted from
  file A and added to file B → one "moved" operation instead of paired
  delete+add. The single biggest mental-model improvement for
  refactor-heavy PRs, and impossible without structural parsing.
- **Sub-symbol rename detection.** Distinguish "function `foo` renamed to
  `bar`, body unchanged" from "`foo` had one line changed".

## Out of scope

- Replacing the LLM as the source of `intent` / `context` / `smells`.
  The structural parse augments the LLM; it doesn't supplant it.
- **Sub-line folding.** Per-line is the floor for diff review (cursor,
  selection, search, and `git diff` all expect the line as the unit). The
  lever is smarter line-level boundaries, which AST folds already deliver;
  collapse-to-glyph and parameter-list elision don't generalise and break
  too many tooling expectations.
- Live ref-link verification ("does the symbol named in `reason` actually
  live at the cited line?"). Plausible follow-on now that symbol indexing
  exists, but unbuilt.
- IDE-style real-time editing. The viewer is a review surface, not an editor.
