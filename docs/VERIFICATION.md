# Verification record — 0.2.0rc5

## Baseline and local environment

The baseline is merged main commit `6afbfe670df2bb79792bc076cf98a2c721d3a439`, tree `8ec8297c7c8a36465ad92d069ec0189c20e1d71e`. The local archive's Git tree was verified before editing. Dependencies came from a read-only GitHub workspace workflow because this container cannot resolve external package hosts; no runtime dependency or lockfile change was needed.

Local checks used Linux x86-64 CPython 3.13.5, real pinned Tree-sitter grammars, HTTPX 0.28.1, Pydantic 2.13.5, PyYAML 6.0.3, Ruff 0.16.8 and ty 0.0.82. Production transport tests use HTTPX MockTransport, not simulated parser trees or a separate request implementation.

## Coordinated checks

```text
uv run --no-sync ruff format --check src tests
uv run --no-sync ruff check src tests
uv run --no-sync ty check
uv run --no-sync pytest -q -rs
uv run --no-sync radon cc -s -n C src
uv build --offline --no-build-isolation
```

Formatting, lint and type checks passed. **223 tests passed, no skips.** Both sdist and wheel built. Radon is an informational report: existing normalization/assessment paths still have nontrivial complexity. New source selection, query construction, request execution and presentation responsibilities have separate owners rather than being added to a generic agent state machine.

A clean installed-wheel smoke test outside the checkout verifies version, YAML round-trip/default null token caps, compaction defaults, entry points, schema-6 reports, and real parsing for all five languages. Final PR CI is authoritative for locked Linux Python 3.12/3.13/3.14 and macOS Python 3.12 verification. Permanent CI remains read-only and does not publish or merge releases.

## Regressions and source examples

The tests exercise an intact file above the old 28k and 46k estimated-token levels being sent to the mock provider without pruning; individual/context and aggregate-question rejection; exact supported error envelopes versus unrelated validation/auth failures; finite decreasing compaction attempts; no replay of identical failed bodies; and complete-target fallback. A rejected file rule cannot silently omit a second file rule whose own request could succeed. File targets are never assessed as fragments.

A large owner with many methods demonstrates per-file rejection hints rather than a full-state retry for each sibling. Identical compacted evidence still batches independent rules. AST checks retain a complete target and referenced local helper/constant, state fields, constructor, and imports while omitting a large unrelated body. Custom YAML instructions are embedded in real auxiliary requests; source-selection call limits and honest coverage are asserted. Original question meaning/target attribution and existing cross-file enrichment/cache/error tests remain covered.

The terminal regression supplies 580 detailed reduction diagnostics and verifies one concise coverage summary in both normal and verbose text, while real syntax errors stay visible and machine diagnostics remain intact. Callback tests cover nested Bun test labels, Promise constructors, nested microtasks and generic argument roles without changing canonical names/IDs or filtering callbacks.

In addition to committed representative fixtures, the exact public `packages/iyon-tui/src/runtime/output-waiter.ts` Git blob `fbf736fd3adeb8e002af850b3bd6586d97e36e2d` was fetched, copied into the local verification workspace, and its Git blob hash checked. Real TypeScript parsing produced 14 units and 13 declaration spans; its three pump callbacks display as `OutputWaitOwner.pump.then[arg1]`, `then[arg1]`, and `then[arg2]`, distinguished by source lines. This source file is not duplicated into the repository or executed. The larger test patterns in the suite are adapted minimal cases, not an entire live iyon-tui scan.

## Research and remaining acceptance

See [CONTEXT_RECOVERY.md](CONTEXT_RECOVERY.md) for primary-source references and the distinction between integration documentation and a captured provider error. No authenticated Jev call was made. The accepted `max_tokens_exceeded` compatibility forms are tested, but not represented as individually observed production responses. The documented Jev-1.13 32k/64k limits mean an approximately 46k-token local estimate is not guaranteed to fit; the change permits provider-authoritative decisions instead of inventing a larger window.

No source-selection accuracy, false-positive/negative rate, live latency, or massive-repository peak-memory benchmark is claimed. Lexical dependency selection is not semantics-preserving program slicing or compiler-backed call resolution. Dynamic dispatch, aliases, hidden side effects and external contracts can be missed; omissions and source provenance remain explicit. One optional model relevance judgment can be wrong. Strict full-target coverage is retained rather than inferred from a smaller selected state.

Syntax errors still stop evaluation of the affected file. Windows and the complete TypeScript repository were not tested. A pinned-model, fixed-source JSONL comparison remains necessary to evaluate semantic usefulness and any production response shape not covered by the compatibility contract. More compact source, fewer diagnostics, or additional auxiliary calls alone do not establish improved judgments.
