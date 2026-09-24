# Verification record — 0.2.0rc7

## Baseline and environment

The baseline is the previously released 0.2.0rc6 revision. This revision
changes execution, caching, budgeting, enrichment policy, reporting, and
documentation; runtime dependencies and the lockfile are unchanged.

Local verification used Linux x86-64 CPython 3.13.5 with the project's locked dependencies and real bundled Tree-sitter grammars. Transport tests use HTTPX `MockTransport`; no authenticated TypeSafe/Jev request was made while developing or verifying rc7.

## Coordinated checks

```text
.venv/bin/ruff format --check src tests
.venv/bin/ruff check src tests
.venv/bin/ty check
.venv/bin/pytest -q -rs
.venv/bin/radon cc -s -n C src
python -m build --no-isolation
```

Formatting, lint, and type checking pass. **246 tests pass with no skips.** Radon remains informational. Both sdist and wheel build successfully.

A clean virtual environment outside the checkout installed `py3_jevscan-0.2.0rc7-py3-none-any.whl` from an offline wheelhouse. The installed CLI reported rc7, resolved the packaged configuration, parsed the real Python, Rust, Perl, TypeScript, and JavaScript fixtures in offline mode, and completed `--plan` without an API key. The plan report made zero live requests and produced a nonzero request estimate. Final pull-request CI remains authoritative for Linux Python 3.12/3.13/3.14 and macOS Python 3.12.

## Cost and execution regressions

The planner now groups checks by exact encoded evidence, independent of rule and target identity. A regression verifies that Noul, Score, and Choice rules for the same owner share one request. Another verifies that an oversized shared owner remains grouped until recovery, and that six sibling rules which converge on the same compacted evidence are repacked into one model request instead of remaining singleton calls.

Large-file execution is no longer limited to one sequential request lane. A mocked 20-request file reaches the configured global concurrency of four while `JevClient` keeps one shared semaphore and one shared request-rate limiter around actual transport starts.

The answer cache has a per-judgment layer keyed by endpoint, requested model, exact effective state (source evidence plus shared rubric registry), exact typed question, and prompt compatibility version. A warm rerun can therefore reuse a judgment after unrelated batch composition changes while the effective state is unchanged; changing selected rubric material changes the state and prevents reuse across that boundary. Tests also cover upgrading an existing schema-1 cache and ensure source content is represented by hashes/opaque keys rather than appearing in SQLite cache keys.

Live budgets are enforced before each transport attempt. Regressions cover request-count and conservative input-cost limits, including refusal to start an attempt that would cross the configured budget. Successful provider usage is reported separately from the conservative reservation estimate. `--plan` performs parsing and initial request packing without creating a Jev client or requiring an API key.

## Enrichment and coverage regressions

Targeted enrichment admits only the evidence families declared by the active rule; `full` restores all available families and `off` disables cross-source enrichment. Built-in unit rules that do not intrinsically require the whole file now start from owner context and request callers/callees/tests/enclosing context only where that evidence class can answer the rule's missing fact.

The configurable full-file line ceiling never substitutes a partial file for a complete-file judgment. Checks that require an oversized complete file are recorded as skipped/incomplete while owner- and unit-level work can continue. Coverage remains explicit in machine reports.

Changed/staged target selection is exercised with a real temporary Git repository. It only changes the selected source set; it does not claim downstream dependency coverage.

## Reporting, cancellation, and compatibility

Interactive text mode receives ephemeral progress events while requests are active; JSON and JSONL reports remain deterministic and do not contain progress heartbeats. Closure targets remain independently analyzed but are indented beneath ordinary targets in text output. Parser worker processes ignore terminal `SIGINT` so the parent process owns cancellation and can emit completed results without child `KeyboardInterrupt` traceback floods.

Provider compatibility tests from rc6 remain in place for HTTP 413, structured `max_tokens_exceeded`, request-local 400/422 isolation, retries, exact target attribution, complete-target recovery, token calibration, and sanitized diagnostics. The report schema is 8; configuration schema remains 4 because the new fields are additive.

## Remaining live acceptance

No TypeSafe credits were consumed for this revision. The next live acceptance should begin with `jevscan --plan`, then a small representative source subset, before attempting a complete repository scan. Record actual `usage.input_tokens`, request density, judgment-cache hits, compaction/enrichment calls, and final cost. The main acceptance question is whether exact-evidence batching and narrower enrichment reduce repeated source transmission enough to make full scans economical without silently reducing semantic coverage.
