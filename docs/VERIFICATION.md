# Verification record — 0.2.0rc3

## Local software checks

Executed in a Linux x86-64 container with CPython 3.13.5, actual bundled Tree-sitter grammars, HTTPX 0.28.1, Pydantic 2.13.5, wcwidth 0.8.4, Ruff 0.16.8, and ty 0.0.82. Dependencies were installed through uv from the retained dependency wheels because the working container cannot access a package index directly. No dependency or lockfile change is required by RC3.

The final coordinated software pass ran:

```text
uv run --no-sync ruff format --check src tests
uv run --no-sync ruff check src tests
uv run --no-sync ty check
uv run --no-sync pytest -q -rs
uv run --no-sync radon cc -s -n C src
uv build --offline --no-build-isolation
```

Formatting, linting, and type checking pass. **166 tests passed, no skips.** Radon was reviewed as an informational report, not a claim that every function has low complexity. Both source and wheel distributions build. The wheel was installed in a separate virtual environment and exercised outside the checkout: package identity, configuration-v3 enrichment reasons and opt-out, schema-4 reports/tentative counters, and real five-language offline parsing pass. Offline work made no provider or enrichment calls.

The baseline is the source tree from merged RC2 (`2a4db57ac18b9f4004d9b73dd60a540ad34e8a88`); its local Git tree matched `7134c9268e35b5784fb87c0fe153fb4852ce5c00` before editing. The PR's final CI run, rather than a local environment claim, is authoritative for locked installation and cross-platform results.

## Behavior covered

RC3 tests exercise:

- Missing evidence at the end of a file receives a scarce review slot before earlier applicability/optional-confidence reviews; final target emission stays in source order.
- Ordinary low-confidence Score/Choice answers and ambiguous Nouls make zero routing calls and do not load the source catalogue. Reduced context still requests review when the displayed reason is low confidence. A specific rule can opt into additional reasons; invalid reason names are rejected.
- Both increasing and decreasing score rubrics preserve warning/error signals below confidence gates. A low-confidence error is not downgraded to a confident warning. Explicit missing-context, N/A and non-defect Choice answers cannot become tentative defects; weak defect probabilities below their gates are not assigned a severity.
- Ambiguous Nouls have tentative severity only when their directional signal actually crosses a configured gate, including `expected: false`.
- Tentative warnings and errors appear without `-v`, using cyan `?`, labelled severity, YAML messages, and an uncertainty reason. Ordinary uncertainty stays verbose-only. Filtering precedes headers/display limits; Unicode-width wrapping and color/no-color output are covered.
- Tentative results are counted once, separately from confirmed findings and within the uncertainty total. They do not trip `--fail-on`. Changing a confidence threshold reclassifies the same cached raw answer from tentative to confirmed without another HTTP call.
- JSON and JSONL preserve complete events and audits independently of verbosity, color and text limits.

Retained integration tests cover real parsing in all five languages and JSX/TSX; spawned-process discovery/parsing; large classes and shared evidence; independent answer bindings; every request budget; reduced context and omitted targets; provider-size recovery; reordered/malformed answers; caching, partial failure and cancellation. The full route/retrieve/relevance/reassess sequence still runs through the production HTTPX client with MockTransport. Candidate selection, unchanged final questions/no prior-verdict feedback, source snapshots, caller changes, ignored/symlinked sources, stopping conditions and enrichment limits remain tested.

Tests establish observable software behavior and contracts. Neither test counts nor mock confidence values measure Jev's semantic accuracy.

## Continuous integration and packages

Permanent CI keeps `uv sync --locked --all-groups`, whole-project Ruff formatting/lint, and ty. Radon is informational. The test/package matrix covers Linux Python 3.12, 3.13 and 3.14, and macOS 14 Python 3.12. Each job requires native parsers, runs the suite, builds distributions, installs the wheel instead of the editable checkout, and smoke-tests from a temporary directory. Distribution artifacts are retained; nothing is published or released automatically.

## Remaining acceptance boundary

No live authenticated Jev request was made for RC3. The prior supplied self-scan motivated the policy change; it is not a fresh test of the new policy or a labelled precision/recall benchmark. Scheduling a previously starved evidence gap does not guarantee the router will choose useful source or that a reassessment will become conclusive. Router thresholds, rule thresholds, and Noul uncertainty intervals are unchanged policy starting points, not calibrated correctness guarantees.

No new semantic question, provider session API, compiler-resolved call graph, module/tree evaluation, or recursive retrieval was added. The source index remains lexical and bounded; it can miss aliases, dynamic dispatch, macros or external contracts. Existing request-token estimates remain heuristic. Windows and massive-repository performance/peak-memory benchmarks were not run.

For live acceptance, pin a model and keep source/configuration fixed. Compare enabled enrichment with `--no-enrichment`, inspect `reviews.trigger` and budget outcomes for missing/reduced evidence, and inspect `tentative_findings` alongside confirmed `findings`. Confirm that high-signal low-confidence results are visible without treating them as verified defects. Fewer auxiliary calls or fewer question marks alone is not evidence of better judgments.
