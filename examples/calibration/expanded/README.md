# Expanded public synthetic calibration corpus

This directory expands the nine packaged JEV rules without changing the original
`examples/calibration/sources/` fixtures or their reviewed snapshot. The
`sources/` directory contains 108 neutral evidence files in 52 semantic
groups after source review. `MANIFEST.yaml` is the review mapping
and is intentionally outside the evidence root.

## Scope and split

JEV01 has four groups; each other rule has six. Development contains 34
groups and heldout contains 18, assigned before any model calls. Source
review consolidated related JEV01 development translations instead of
counting them as independent support. A repair, counterpart, or translation has the same `scenario_group` as
the case it clarifies and therefore counts once. Each mapping entry records the
exact rule, source path, target scope/name/kind, applicability facts,
provisional label, rationale, and context requirements.

The source files deliberately do not contain provisional labels or answer-shaped
comments. The labels are development annotations only; they are not independent
ground truth and no live model calls are authorized by this corpus.

The groups cover:

- JEV01 through JEV03 with interleaved responsibilities, control-flow
  transitions, and abstraction boundaries;
- JEV04 and JEV05 with redundant checks, visible boundaries, invariant
  failures, explicit errors, and missing-contract ambiguity;
- JEV06 with meaningful and forwarding helpers across classes, packages, and
  Rust impls;
- JEV07 and JEV09 at file scope, contrasting competing state owners and
  duplicated implementations with deliberate boundaries;
- closures and callbacks, generators, destructuring, async operations,
  lifecycle methods, Perl package methods, JavaScript and TypeScript classes,
  Rust ownership-oriented code, and Python context managers.

All five parser-supported languages represented by the existing corpus are used:
JavaScript, TypeScript, Python, Perl, and Rust.

These are semantic source excerpts, not a runnable application. Some omit
collaborator implementations or initialization, including codec helpers and
injected service attributes. A missing implementation does not establish a
guarantee merely because a helper has a suggestive name. Reviewers must use
`Partial` when the exact rule depends on a contract absent from the evidence.
Parsing and planner eligibility do not establish runtime correctness.

## Offline verification

From the repository root:

```bash
uv sync --locked
uv run jevscan examples/calibration/expanded/sources --offline
uv run jevscan examples/calibration/expanded/sources --plan
```

The production parser and `Planner` must accept every mapped target. The
expansion manifest digest uses the same canonicalization as the legacy manifest:
sorted source filenames, each followed by a NUL byte and the raw UTF-8 bytes.
The focused corpus tests also verify the reviewed group counts and split,
neutral source names, source digest, and target applicability.
Frozen source evidence is excluded from application linting, formatting, and
type checking; those tools must not rewrite deliberate defects or source hashes.

Do not treat the provisional mapping as a model score or as a replacement for
blind source review. Independent reviewers should adjudicate the source files
without consulting labels first.
