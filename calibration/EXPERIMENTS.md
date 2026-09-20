# Historical JEV01/JEV02 calibration experiment

This page preserves reproduction instructions for an earlier experiment,
not the supported calibration workflow. Its alternative questions and error
policies were diagnostics; they are not candidates for the current
warning-only selection. Use the [portable skill](../skills/jevscan-calibration/SKILL.md)
for new project calibration and [FINAL_EVALUATION_PLAN.md](FINAL_EVALUATION_PLAN.md)
for the final built-in evaluation.

`experiment.py` is a bounded, reproducible live harness. It contains no
private source or adjudicated labels. A local manifest supplies exact source
roots, source/target SHA-256 values, owner groups, split membership, and blind
review judgments. The manifest and all JSONL case/receipt output must remain
ignored and local.

The harness uses the production parser and context builder to make the same
evidence for every wording bundle. Labels are not included in request state or
question instructions. Development uses three live rounds: baseline; one
combined round containing both balanced JEV01 variants; and the clarified JEV02
bundle. It produces four fixed candidate bundles:

* `baseline`: packaged JEV01/JEV02 questions;
* `jev01-balanced-a`: target-itself/policy-state wording with explicit
  orchestration, cleanup, metrics, and phase exclusions;
* `jev01-balanced-b`: target-itself/interleaving/material-harm wording with
  the same exclusions; and
* `jev02-clarified`: packaged JEV01 plus an explicit level-1 traceability
  clarification.

No packaged rule or report threshold is edited. JEV02 reporting includes exact
score agreement and a separately named levels-2-plus-3 probability-mass
diagnostic. Confirmed and tentative signals are retained separately; there is
no fixed accuracy gate.

## Commands

```console
# Validate exact targets and ensure no source hash or owner-group leakage.
uv run python calibration/experiment.py validate \
  --manifest .jevscan-calibration/manifest.json

# Preflight without credentials or network access.
uv run python calibration/experiment.py dev \
  --manifest .jevscan-calibration/manifest.json --plan

# Development call. The script reads TYPESAFE_API_KEY and optionally
# TYPESAFE_BASE_URL; it never prints or stores the key.
uv run python calibration/experiment.py dev \
  --manifest .jevscan-calibration/manifest.json

# Select reporting-only thresholds from the already captured development
# answers. This makes no API calls.
uv run python calibration/experiment.py select \
  --cases .jevscan-calibration/dev-cases.jsonl

# Freeze the baseline question plus the selected reporting policy before any
# heldout call. The selection file contains the exact policy hash.
uv run python calibration/experiment.py freeze \
  --ledger .jevscan-calibration/dev-ledger.json \
  --candidate baseline \
  --selection .jevscan-calibration/dev-selection.json

# The heldout command reads the frozen candidate and cannot select another.
uv run python calibration/experiment.py heldout \
  --manifest .jevscan-calibration/manifest.json \
  --freeze .jevscan-calibration/freeze.json
```

The client uses model `jev-1.13.0`, input price `$0.042/M`, a task-wide
reserved spend ceiling below `$1.00`, and a small safety margin. Receipts
record request/evidence/body hashes, reserved and reported tokens, and costs;
they do not retain request bodies.

The heldout phase makes one baseline-question request per evidence batch.
Baseline assessment and the frozen reporting-only policy are replayed from
those same heldout answers; no duplicate heldout API calls are made.

## Generic manifest capture/import

`capture.py` is the reusable owner for public or project-supplied calibration
manifests. It accepts a YAML manifest with a relative `source_root`, stable
scenario IDs/groups, exact targets, and rule IDs. Use `--config` for a project
configuration; otherwise the packaged rules are used.

```console
# Production parse/planner validation and a no-network estimate.
uv run python calibration/capture.py plan \
  --manifest examples/calibration/MANIFEST.yaml \
  --snapshot-sha256 <reviewed-snapshot-hash>

# Capture unlabeled answers only after an explicit budget/credential decision.
uv run python calibration/capture.py capture \
  --manifest examples/calibration/MANIFEST.yaml \
  --snapshot-sha256 <reviewed-snapshot-hash> \
  --live --max-cost 0.05

# Import only finalized blind adjudications. Provisional manifest outcomes
# are never used as ground truth.
uv run python calibration/capture.py import \
  --manifest examples/calibration/MANIFEST.yaml \
  --answers .jevscan-calibration/synthetic-answers.jsonl \
  --adjudications calibration/synthetic-adjudications.json
```

Capture requests contain production questions and evidence only; labels,
provisional outcomes, and adjudication reasons stay outside the provider
request. The import summary keeps applicability skips, `insufficient_context`,
exact expected answers, scenario groups, and leave-one-group-out support
separate. Paired or translated scenarios are never split or counted as
independent heldout evidence.

## Final scan capture

For a real scan, `jevscan --calibration-output PATH` records the final
`FileResults` judgments after optional enrichment. It is opt-in; normal scan
reports and source handling are unchanged. The capture includes exact final
evidence snapshots, the final primary question wire, returned model, final
assessment, and review disposition/question wires. Source snapshots are
written only to the explicitly requested capture.

External labels are imported with the installed command:

```console
jevscan --calibration-output .jevscan-calibration/final.jsonl \
  --format jsonl -o report.jsonl path/to/project
jevscan-calibration-import .jevscan-calibration/final.jsonl \
  --labels final-labels.yaml \
  --output .jevscan-calibration/final-cases.jsonl
```

The final capture path is distinct from the generic initial
Planner/evidence-only capture above. It is the authoritative integration point
for full-scan/enrichment judgments; it does not reconstruct an initial
planner state or run a second scoring path.
