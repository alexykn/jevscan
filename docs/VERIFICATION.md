# Verification record — 0.2.0rc4

## Local checks

Executed on Linux x86-64, CPython 3.13.5, with real bundled Tree-sitter grammars, HTTPX 0.28.1, Pydantic 2.13.5, Ruff 0.16.8 and ty 0.0.82. The starting source tree was checked against merged RC3 commit `3f7ca8dcfb5e19fa88ade1352a418a2ca2bf894d`, tree `f10345c75cf56595a6f8695940a415edf2a140fc`.

The container could not access a package index directly. Existing verified dependency wheels were reused; the new Tomli-W wheel and updated registry-backed uv lockfile were resolved by an isolated GitHub dependency-preparation run and downloaded through the connector. Tomli-W 1.2.0's SHA-256 was checked against published PyPI metadata. PyYAML is no longer a runtime dependency; the unrelated bundled Tree-sitter YAML grammar remains a transitive parser dependency.

The coordinated checks were:

```text
uv run --no-sync ruff format --check src tests
uv run --no-sync ruff check src tests
uv run --no-sync ty check
uv run --no-sync pytest -q -rs
uv run --no-sync radon cc -s -n C src
uv build --offline --no-build-isolation
```

Formatting, lint, and type checking passed. **186 tests passed, no skips.** Radon was reviewed as an informational report; no claim is made that every existing function has low complexity. The new disposition/family helpers are separate from candidate ranking and file-local result ownership.

Both sdist and wheel built. A clean, separate virtual environment installed the wheel and was exercised outside the source checkout: package identity, absence of a PyYAML import, minimal TOML initialization, resolved TOML, enrichment opt-out, rule/set selection, schema-5 reporting, and all five real native grammar frontends passed. Both checked-in example TOML files resolve successfully. A direct comparison confirmed that all nine original primary questions, criteria, numerical reporting thresholds, and uncertainty/enrichment admission policies were preserved exactly under their new IDs.

## Behavioral coverage

Configuration tests exercise empty/additive project configs, stable named rule overrides, new rules and sets, rule/set disablement, ignore/select precedence, effective planner selection, unknown selectors, duplicate names, invalid report/budget contracts, legacy YAML detection, Git discovery boundaries, and resolved TOML round trips. The CLI exposes stable IDs/titles/membership without turning display metadata into model instructions.

Routing tests use the actual HTTPX client with MockTransport. They cover multiple simultaneously useful evidence families in one routing request, no qualifying families, high speculative family scores that cannot override a terminal disposition, low disposition confidence, family deduplication, a shared global candidate cap with fair pooling, preserved per-family omission provenance, small question/call budgets, partial routing audits, and exactly one unchanged-question reassessment. Changed callers invalidate affected relevance/final requests while eligible initial/routing answers remain reusable.

Retained tests exercise intrinsic-uncertainty admission, priority for late missing evidence, unknown/applicability handling, tentative warning/error visibility, confirmed-only exit behavior, Unicode/ANSI-safe wrapping, complete machine reports, exact source spans, real five-language parsing, spawned-process execution, source restrictions, context reduction, provider failures, and cancellation. Test counts and mocked confidence values are not semantic accuracy metrics.

## Continuous integration

Permanent CI retains read-only repository permissions and locked dependency installation. It checks the whole project and tests/builds/smoke-tests installed wheels on Linux Python 3.12, 3.13, 3.14 and macOS 14 Python 3.12. Final PR checks, rather than the local environment, are authoritative for that matrix. Distribution artifacts are retained; nothing is automatically merged or published.

## Acceptance boundary

No authenticated Jev request was made for RC4. The real user's prior scan motivated this change, but its display did not include the router's underlying probability distribution. Competition between several useful families was a design hypothesis, not a measured cause. Independent family questions remove that false exclusivity without proving that Jev will choose better evidence or become more certain.

Remaining limits include lexical rather than compiler-resolved references, aliases/dynamic dispatch/macros/external contracts, per-file rather than atomic repository snapshots, heuristic token sizing, and bounded discovery. Windows and massive-repository peak-memory/performance were not tested.

For live acceptance, pin a model and preserve source/configuration; save JSONL from representative known-positive/negative examples with and without enrichment. Inspect disposition and family probabilities, candidate provenance, selected source and stop outcomes. Improved final correctness matters more than more retrieval calls or fewer question marks. Threshold tuning remains a separate project policy decision; no numeric finding defaults were retuned here.
