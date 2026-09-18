# Verification record — 0.2.0rc1

## Local software checks

Executed in a Linux x86-64 container with CPython 3.13.5, actual bundled Tree-sitter grammars, HTTPX 0.28.1, Pydantic 2.13.5, wcwidth 0.8.4, Ruff 0.16.8, and ty 0.0.82. Dependencies were installed from downloaded wheels because the working container cannot access a package index directly.

The following checks were run against the refactored production code:

```text
ruff format --check src tests
ruff check src tests
ty check
pytest -q -rs
radon cc -s -n C src
uv build
```

Formatting, linting, type checking, and tests pass. The suite includes real parsing for all five languages, JSX/TSX, and the production spawned-process pipeline; parser tests were not skipped. Both source and wheel distributions were built. The PR's CI results are the authoritative record for the locked dependency environment and cross-platform runs.

The new regressions exercise shared owner/file evidence, a class larger than 32 KB, independent method bindings, Rust sibling declarations/impls, file targets without lexical units, Unicode byte ranges, exact request byte accounting, question-budget splitting, reduced-context disclosure, whole-target omissions, strict context policy, bounded provider size recovery, response-ID validation, reordered answers, cache reclassification, and partial results on abort. The pipeline tests retain the 80-file concurrency/cache fixture. Terminal tests cover warning/error-only defaults, verbose unknown/OK results, display-limit ordering, always-visible diagnostics, unlimited JSON/JSONL, Unicode/ANSI hanging indents, long unbroken words, and escaped terminal controls.

Tests use the actual HTTPX client with MockTransport. Synthetic parser fixtures are limited to queue/cache isolation tests; separate integration cases use the actual native parser. Neither task-count assertions nor the self-scan are large-repository performance/accuracy benchmarks.

## Continuous integration and distribution checks

The workflow uses a committed `uv.lock` with `uv sync --locked --all-groups`. It checks Ruff formatting/lint and ty across the **whole source/test tree**, not just selected renderer files. Radon produces an informational complexity report; no numerical complexity threshold is represented as a correctness guarantee.

The test/package matrix targets Linux on Python 3.12, 3.13, and 3.14, plus macOS 14 on Python 3.12. Each job explicitly requires native parser dependencies, runs the test suite, builds distributions, replaces the editable install with the built wheel, changes to a temporary directory, verifies the actual site-packages import, and runs the CLI/configuration commands and five-language offline parsing. Distribution artifacts are retained for review; the workflow does not publish to PyPI or create a release.

## Not established by these checks

No live authenticated Jev request was made for this release candidate. The tests do not establish provider availability, exact token limits, account quotas, long-context semantic accuracy, or stability under larger shared states. The local token estimator is a configurable heuristic, not a provider tokenizer. Software tests verify explicit size-rejection recovery and coverage reporting, not that arbitrary 10,000-line files fit the model.

Module/tree analysis, cross-file symbol resolution, macro expansion, call-graph construction, and whole-repository architectural judgments are not implemented. Oversized whole targets are not silently clipped or scored from fragments. Default warning/error boundaries are review-policy choices, not calibrated defect probabilities; wider evidence and file targets require their own semantic evaluation.

No massive-repository throughput/peak-memory benchmark or Windows run was performed. Parser/read/queue/evaluator limits bound admitted work, but configured file size, nesting, and question count still affect memory and latency. Cache/model reproducibility requires a pinned model rather than a moving provider alias.

## Optional live acceptance check

After reviewing source-sharing policy and selecting an account-supported model, run a small representative project both normally and with `-v`. Inspect JSONL `target`, `evidence`, and `skipped_rules`, particularly on classes with helpers and Rust files with multiple impl blocks. Compare known-positive, known-negative, and insufficient-context cases before making semantic findings a mandatory merge gate. A passing offline suite is not a substitute for this provider-level acceptance check.
