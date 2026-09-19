# Verification record — 0.2.0rc6

## Baseline and local environment

The baseline is merged main commit `c8ec885be94265a9f6a237ca8ab7b088e2f9cb94`, tree `95a5437e303ef7955018e566e2b0e1c91109b064` (merged rc5). The local source was reconstructed from the prior verified rc5 artifact; all non-workflow source matched the merged tree, and the permanent read-only CI workflow was synchronized from main before editing. No runtime dependency or lockfile change is required for rc6.

Local checks used Linux x86-64 CPython 3.13.5, the pinned native Tree-sitter grammars, HTTPX 0.28.1, Pydantic 2.13.5, PyYAML 6.0.3, Ruff 0.16.8 and ty 0.0.82. Transport tests use HTTPX MockTransport; parser tests use the real bundled grammars.

## Coordinated checks

```text
.venv/bin/ruff format --check src tests
.venv/bin/ruff check src tests
.venv/bin/ty check
.venv/bin/pytest -q -rs
.venv/bin/radon cc -s -n C src
python -m build --no-isolation
```

Formatting, lint and type checks passed. **229 tests passed, no skips.** Both sdist and wheel built. Radon remains informational; the touched request-execution and file-finalization paths were decomposed so the new preflight/rejection handling is not concentrated in one large orchestration method.

A clean installed-wheel smoke test outside the checkout verified `0.2.0rc6`, YAML round-trip/default 28k/56k planning thresholds, schema-7 reports, the command entry point, and real five-language offline parsing. Final PR CI remains authoritative for Linux Python 3.12/3.13/3.14 and macOS Python 3.12. Permanent CI is read-only and does not publish or merge a release.

## Regressions and failure transitions

The model-budget tests assert that Jev-1.13/`jev-latest`/`jev-preview` use the published 32k state+longest-question and 64k aggregate ceilings even when local token caps are `null`, while the packaged defaults preserve 28k/56k headroom. Explicit values above the known ceiling are capped; unknown future model IDs do not receive invented limits. Run-local token calibration is one-way: observed `usage.input_tokens` can only tighten the byte/token estimate.

A >46k estimated complete-file judgment now fails preflight without an HTTP request rather than discovering a known limit at the provider. Aggregate/question-count overflow is split without reducing source. Large unit contexts exercise the AST compactor before HTTP and preserve the complete target, referenced helper/constant, owner state/constructor and imports while excluding a large unrelated body. Custom YAML questions still appear verbatim in auxiliary relevance bindings; no built-in rule ID carries hidden semantics.

Provider compatibility tests cover HTTP 413, top-level/nested `max_tokens_exceeded`, and nested validation `detail[].type=max_tokens_exceeded`. Free-text `message`/`msg` token mentions do not enter size recovery. Unknown HTTP 400/422 responses expose only bounded machine fields, sanitized request ID and a response fingerprint; source-bearing messages are not retained. One request-local rejection omits only its checks and later requests in the file continue. The third equivalent request-local rejection trips the shared circuit breaker and becomes scan-fatal.

Coverage presentation has a regression for the exact rc5 failure shape: `aborted: true`, zero compacted targets and 1,190 unfinished checks renders as **scan aborted**, never as “context compacted for 0 targets.” The existing 580-detail aggregation test still verifies that routine context diagnostics collapse to one file summary while syntax/API errors and machine diagnostics remain visible.

Existing regressions for bounded provider-size recovery, no replay of identical rejected bodies, complete-file/complete-target guarantees, cache identity, cross-file enrichment, callback display names and source provenance remain green.

## Research and remaining acceptance

See [CONTEXT_RECOVERY.md](CONTEXT_RECOVERY.md) for the research basis. Pydantic's current TypeSafe integration documentation describes Jev 1.13 as 32k state+longest-question / 64k aggregate and names `max_tokens_exceeded`; the official TypeSafe Python SDK still exposes generic HTTP error machinery rather than a dedicated public context-limit envelope. No authenticated oversized Jev request was made while building rc6, so the exact production error body that caused the user's rc5 HTTP 400 remains unobserved.

The next live acceptance run should use the same fixed `iyon-tui` revision and save JSONL. Success criteria are: known large contexts invoke preflight compaction rather than generic 400 failure; request-local 400/422 failures do not cancel unrelated files; any real `max_tokens_exceeded` envelope is recognized or yields enough safe machine metadata to add exact compatibility; compacted source remains relevant; and overall findings remain useful. Fewer omitted checks or more auxiliary calls alone are not semantic validation.
