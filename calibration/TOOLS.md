# Historical offline calibration tools

The commands on this page reproduce the original cache-based experiment;
they do not make provider requests. Other research scripts in this directory
can make explicitly authorized live requests. These repository-only tools are
not installed or supported as an alternative project calibration workflow.

For current calibration, follow the [portable skill](../skills/jevscan-calibration/SKILL.md)
using the installed capture, import, and selection commands. Historical
sweeps below can vary error policies; the production selector cannot and
must not do so.

## Capture

`capture_cache.py` requires a clean Git checkout and copies both the checkout
and the authorized SQLite cache into a temporary staging tree. It blocks
`httpx.AsyncClient.post` before invoking the normal `jevscan` runner. Missing
cache entries fail closed by default and mark the capture incomplete.
The explicit exploratory `--allow-synthetic` mode permits marked synthetic
answers so the runner can continue; those answers are never imported as model
judgments, and the capture remains partial.

Run it with the package source that matches the frozen checkout:

```console
PYTHONPATH=/path/to/frozen/src:$PWD/src \
  uv run --no-sync python calibration/capture_cache.py \
  /path/to/frozen-checkout \
  /path/to/frozen-checkout/.jevscan-cache/results.sqlite3 \
  --events calibration/baseline.events.jsonl \
  --capture calibration/baseline.capture.json
```

The source checkout and cache are read-only inputs. The capture JSON and event
JSONL are intermediate material and should remain ignored or local. Inspect
`transport_blocked`, `source_commit`, `synthetic_requests`, and the runner
summary before importing anything.

## Import

`import_cases.py` requires an ordered JSON array. Each row must contain:

```json
{
  "display_id": 1,
  "target": {
    "id": "tests/example.py:10:function",
    "scope": "unit",
    "path": "tests/example.py",
    "language": "python",
    "qualified_name": "example",
    "start_byte": 0,
    "end_byte": 10,
    "start_line": 1,
    "end_line": 1,
    "kind": "function",
    "display_name": "example"
  },
  "rule_id": "JEV01",
  "label": "Partial",
  "explanation": "The displayed signal is related but overstates the rule claim."
}
```

The `target` object must be copied from the captured runner event, not
reconstructed from a line number. `display_id` values must be exactly
`1..N` in the input order. The importer never sorts targets, applies a label
from an answer, or guesses a missing explanation. It verifies the source Git
commit, cached answer identity, evidence/source slices, question hash, and
all other version-1 case invariants through `CalibrationCase`.

```console
uv run python calibration/import_cases.py \
  calibration/baseline.capture.json \
  calibration/baseline.events.jsonl \
  /path/to/frozen-checkout \
  calibration/baseline-labels.json \
  --output calibration/public-development.jsonl
```

If the original displayed ordering or target mapping is unavailable, keep the
capture as unlabeled raw material and stop before this command. A cache hit
does not establish a semantic label.

The normal importer rejects any partial capture. For an exploratory capture
that contains unrelated uncached follow-up requests, `--allow-partial-selected`
is an explicit exception: every selected row must still resolve to a concrete
pre-existing cached answer, and the resulting case provenance is marked
`capture_scope: selected-initial-cache-records`. This mode must not be
reported as a complete scan.

Each imported case also receives a canonical `provenance.source_group` in the
form `jevscan@<frozen-commit>:<target.path>`. This deliberately treats all
targets from one frozen source file as one evidence group; it does not claim
that cases within a file are independent observations.

## Reporting-only sweep

`sweep_thresholds.py` loads strict JSONL cases and calls the existing offline
`replay_cases` path. It changes report thresholds only; it does not alter
questions, evidence, answers, prompts, endpoints, models, or comparability.
Every sweep retains both confirmed and tentative outcomes.

```console
uv run python calibration/sweep_thresholds.py \
  calibration/public-development.jsonl \
  --field warning.min_probability \
  --thresholds 0.50,0.55,0.60,0.65,0.70,0.75,0.80,0.85 \
  --output calibration/public-development.sweep.json
```

The sweep output stores compact deterministic aggregates for each threshold,
including confirmed and tentative counts.
The full source/evidence/question provenance remains in
`public-development.jsonl` rather than being duplicated for every threshold.

For the JEV02 policy comparison requested by calibration, use
`compare_jev02.py`. It keeps the existing expected-score policy as one row and
compares it with `score_levels: [2, 3]` for warning and `[3]` for error over
probability-mass thresholds. It reports confirmed precision, tentative label
mix, and confirmed/review exact-positive recall. When supplied, parent
adjudications are reported in a separate view and do not alter the corpus
labels:

```console
uv run python calibration/compare_jev02.py \
  calibration/public-development.jsonl \
  calibration/baseline-labels.json \
  calibration/adjudications.json \
  --output calibration/jev02-comparison.json
```

`verify_displayed_match.py` checks every selected case's cached raw answer
against the original displayed rounded answer and replays the current
assessment to compare confirmed versus tentative status:

```console
uv run python calibration/verify_displayed_match.py \
  calibration/public-development.jsonl \
  --output calibration/displayed-match.json
```
