# Public synthetic calibration examples

This directory is a small, public example corpus for the nine packaged JEV rules. The
source files are intentionally ordinary code, including the behavior that a rule is
meant to examine. They are not a linter-clean project and should not be rewritten by
an autoformatter before scanning.

`sources/` is the only directory to scan. `MANIFEST.yaml` is the provisional label
record and is deliberately outside model evidence. A scenario's `scenario_group`
identifies paired or translated versions of one design; those versions are one
scenario, not independent held-out evidence. The manifest is for generation review
and calibration setup, not a claim about model accuracy.

## Offline first

From the repository root, after installing the project:

```bash
uv sync --locked
uv run jevscan examples/calibration/sources --offline
uv run jevscan examples/calibration/sources --plan
```

Both commands parse with the real Tree-sitter grammars. `--offline` does not make
API calls or write a cache. `--plan` performs initial target/evidence packing and
reports conservative request and input-cost estimates without an API key.

The manifest records the exact unit or file target expected from planning. A useful
local check is to compare the offline inventory and plan output with
`MANIFEST.yaml`; deterministic syntax exclusions are `not_applicable`, while a
semantic clean example is still an applicable model question.

## Optional live run

Live inference is not required to use the corpus. If you choose to run it, provide
your own TypeSafe credential through the environment and do not put it in this
repository:

```bash
export TYPESAFE_API_KEY="$YOUR_LOCAL_KEY"
uv run jevscan examples/calibration/sources \
  --max-requests 100 \
  --max-input-tokens 250000 \
  --max-cost 0.05 \
  --enrichment-mode off \
  --format jsonl \
  -o /tmp/jevscan-calibration.jsonl
```

The request, input-token, and cost limits are explicit safety bounds, not an
accuracy protocol. Start with one or two files and a single rule when exploring a
new model. A budget stop or provider failure is incomplete coverage. Record the
actual provider usage separately from the conservative plan estimate, and do not
turn a live response into a ground-truth label without blind adjudication.

## Corpus design and limits

- `MANIFEST.yaml` copies the packaged rule proposition, target gate, report message,
  threshold, and severity contract where the built-in configuration provides one.
- Each scenario records source path, extracted target name/kind/scope, expected
  applicability, provisional semantic outcome, rationale, and the context needed to
  judge it. Choice-rule scenarios include explicit missing-evidence cases.
- JavaScript and Perl carry most of the examples. Python, Rust, and TypeScript are
  included where they exercise a distinct target kind, closure, owner, or parser
  boundary.
- Files are intentionally independent. The corpus does not provide a resolved
  cross-file call graph, external contracts, tests, or runtime behavior. Those
  omissions are part of the `insufficient_context` examples where stated.
- Independent scenario counts are intentionally uneven: JEV01 and JEV05 have
  additional paired groups, while the other rules remain a small starting sample.
  This is not complete coverage of every language construct or domain contract.
- Labels were generated synthetically by a language model and are not independent
  ground truth. They are provisional review prompts only. No model accuracy,
  precision, recall, or severity calibration should be inferred from this corpus
  before blind adjudication.

The manifest's `provenance` field is repeated at the corpus level and on each
scenario so copied records retain that warning.
