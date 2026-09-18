# Verification record — 0.2.0rc5

## Baseline and environment

The baseline is `alexykn/jevscan` main commit `6afbfe670df2bb79792bc076cf98a2c721d3a439`, with verified source tree `8ec8297c7c8a36465ad92d069ec0189c20e1d71e`. No runtime dependency was added. Configuration remains YAML version 4; reports advance to schema 6 and the prompt/cache identity to version 4.

Local checks ran on Linux x86-64, CPython 3.13.5, the actual bundled Tree-sitter grammars, HTTPX 0.28.1, PyYAML 6.0.3, Pydantic 2.13.5, Ruff 0.16.8 and ty 0.0.82. The container could not directly access the package index, so dependency wheels and the pinned source were exported through an isolated read-only GitHub Actions workspace run and installed with uv. Final CI is authoritative for the committed lockfile/platform matrix.

## Coordinated software verification

```text
uv run --no-sync ruff format --check src tests
uv run --no-sync ruff check src tests
uv run --no-sync ty check
uv run --no-sync pytest -q -rs
uv run --no-sync radon cc -s -n C src
uv build --offline --no-build-isolation
```

Formatting, lint and type checking pass. **216 tests pass with no skips.** Radon is an informational complexity review, not a numerical acceptance guarantee. The new file executor separates full requests, bounded preparation, repacking and retries; the old file/owner/target fallback and clipped import-string path are removed.

Both source and wheel distributions are built. The installed wheel is checked in a separate environment outside the checkout for package identity, YAML defaults and round-trip, explicit numeric limits, opt-out, schema-6 JSON, and actual five-language offline parsing. CI also builds and smoke-tests installed wheels on Linux Python 3.12/3.13/3.14 and macOS Python 3.12. No workflow automatically publishes or merges the release candidate.

## Focused regressions

Tests cover a full file exceeding the former ~46k estimate being submitted intact to an accepting mock; explicit local ceilings; exact request bytes; question splits; recognized size failures; bounded repeated rejection without identical primary-body retries; whole-file omission instead of partial scoring; retention of complete targets plus fields/helpers/captured declarations; source-byte and Unicode span integrity; auxiliary query semantics for a custom Choice rule; and changed criteria invalidating affected cached requests.

Callback tests retain canonical IDs/qualified names while displaying literal test titles, Promise executors and syntactic callback positions. Actual parser snapshots, including context blocks, survive the process-boundary serialization contract. Tests verify one coverage notice for hundreds of target diagnostics without hiding syntax errors, while JSON/JSONL retain all events. Unknown 422 validation/prose errors do not trigger source reduction; 529 overload is retried rather than treated as a size failure. Raw echoed input is not copied into audits/errors.

Retained integration tests exercise the production HTTPX path with MockTransport, spawned parser workers, five native grammars, JSX/TSX, scoped source discovery, multi-family enrichment, callback/source references, source snapshot restrictions, cache reclassification, tentative severity, incomplete results and cancellation. They are software tests, not measurements of Jev's judgments.

## Larger TypeScript source exercise

The read-only reference workspace exported `alexykn/iyon-tui` branch `agent/dom-occurrence-runtime` at `9de8707389d106655fde61146b5ffffb0de2dc7d`. This remote snapshot differs from parts of the user's local report; in particular, the earlier syntax-error filename was absent. No claim is made to have reproduced that missing file's parser error.

The production parser/planner/executor was exercised against three unmodified files with a deterministic **mock** that rejected states above 40,000 serialized characters, accepted smaller states, and returned typed synthetic judgments. This is deliberately not presented as TypeSafe's tokenizer or semantic acceptance:

| Source | Bytes | Units | Checks |
|---|---:|---:|---:|
| src/react/commit.ts | 83,357 | 178 | 830 |
| tests/tui_react_renderer.test.ts | 89,057 | 279 | 1,312 |
| src/runtime/output-waiter.ts | 3,642 | 14 | 54 |

The large files exercised full-state rejection and AST preparation while retaining target completeness; each produced one coverage event. The small file needed no compaction. Callback labels were derived from source, not model output. Giant complete targets remained omitted rather than being scored from fragments. These files were used as local acceptance inputs, not copied wholesale into the test suite.

## Research and remaining acceptance

Official TypeSafe Models, API, SDK exceptions, State, Noul, fan-out, reranking, Jev 1.13 jaggedness and OpenAPI materials were successfully retrieved through the reference workflow. The model limits are documented as 32k state-plus-longest-question and 64k aggregate; the public schema does not specify a canonical token-limit payload. [CONTEXT.md](CONTEXT.md) distinguishes documented facts from compatibility error shapes and implementation choices.

**No authenticated Jev request was made.** Mocks establish finite transitions, attribution, coverage, caching and packaging, not whether relevance selection improves model accuracy. Unknown provider size-error encodings still fail visibly rather than being guessed. Exact tokenizer counts, account-specific limits, live long-context quality, aliases/dynamic dispatch, external contracts, Windows, and massive-repository peak memory were not verified.

Live acceptance should pin a model, use labelled examples with and without relevant external context, retain JSONL, and inspect preparation/review provenance and final correctness. Fewer warnings, fewer question marks or merely successful request admission is not proof of a better analysis.
