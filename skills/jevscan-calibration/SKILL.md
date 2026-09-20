---
name: jevscan-calibration
description: Evaluate and calibrate a supplied jevscan YAML ruleset using source-based Agree, Disagree, and Partial labels. Use when investigating noisy or missed findings, measuring rule accuracy, or automatically tuning project-level warning thresholds. Not for changing rule meanings or fixing the scanned code.
compatibility: Requires an installed jevscan version with final-judgment capture, calibration import, and automatic policy selection. Live scans require an authorized Jev credential and explicit spending budget.
---

# Jevscan calibration

Calibrate warning thresholds in the user's supplied YAML. Keep rule questions,
error thresholds, and scanned source unchanged. Use jevscan's tools for
capture, validation, selection, and write-back; do not implement another scorer.

## 1. Establish scope

Identify the supplied YAML, authorized source roots, concrete model version,
local artifact directory, and live request/token/cost limits. Do not read other
repositories or spend money without authorization. Never put credentials in a
command, artifact, or response.

Check the installed commands:

```sh
jevscan --help
jevscan-calibration-import --help
jevscan-calibrate --help
```

If final capture or automatic selection is unavailable, explain the version
requirement. Do not substitute reconstructed cache records or initial-only
queries while claiming to capture final enriched scan judgments.

## 2. Capture representative evidence

First run offline extraction and a plan with the supplied configuration.
Resolve parser failures and inspect estimated cost before a live scan.

Use the authorized limits, source paths, model, YAML, and supplied ruleset
name in place of the placeholders below. Repeat `--rule` for multiple rules
or rulesets; supplying YAML alone does not disable merged built-in rules.

```sh
jevscan SOURCE --config RULES.yaml --rule RULESET --offline
jevscan SOURCE --config RULES.yaml --rule RULESET --plan
jevscan SOURCE --config RULES.yaml --rule RULESET --model MODEL \
  --max-requests REQUEST_LIMIT --max-input-tokens TOKEN_LIMIT \
  --max-cost COST_LIMIT --format json --output RUN/scan.json \
  --calibration-output RUN/judgments.jsonl
```

Capture files contain source code. Keep them and their metadata local and
ignored unless publication is explicitly authorized. Use distinct output
paths; do not overwrite source, configuration, labels, or previous evidence.

Keep the capture and its completion metadata together. An incomplete scan
does not establish complete coverage. Do not automatically bypass incomplete
capture rejection.

## 3. Review source, not confidence

Read [the labeling contract](references/labels.md) before labeling.
For every judgment being reviewed, inspect its exact captured target, rule,
and supplied context. Review confirmed and tentative findings, plus
non-findings so missed positives can be discovered.

Label whether the **rule's defect claim** is supported, not whether the
model's particular answer was correct. Preserve ambiguous cases as `Partial`.
Missing evidence is not proof of a defect or of its absence.

Use stable captured case IDs. Record a concrete explanation for each label.
Do not copy expected labels from a fixture generator as independent review.
For independent review, withhold model scores and intended fixture labels
until the reviewer has judged the source.

Assign explicit source groups. Keep the same owner, paired corrections,
translations, and template-derived variants together. Split whole groups into
development and held-out data before selection; never tune on held-out labels.
If support is too small, report that limitation rather than inventing an
independent split.

## 4. Validate and select automatically

Import the labels with the installed tool:

```sh
jevscan-calibration-import RUN/judgments.jsonl \
  --labels RUN/labels.yaml --output RUN/cases.jsonl
```

Resolve validation failures from the actual source of the mismatch. Never
change hashes, case IDs, or completion flags merely to make import succeed.

Run the selector against the supplied YAML:

```sh
jevscan-calibrate RUN/cases.jsonl --select --rules RULES.yaml \
  --development-split development --heldout-split heldout \
  --selected-policy RUN/selected.yaml --output RUN/audit.json
```

Omit `--heldout-split` when no independent held-out data exists and say so.
Use `--rule-id` when only particular supplied rules are in scope.

Inspect the audit rather than manually choosing a threshold:

- Compare baseline and selected development outcomes separately from held-out
  outcomes.
- Report confirmed false positives, tentative label composition, and missed
  positives, not just precision or fewer warnings.
- Check independent support, incompatible records, and search truncation.
- Distinguish a retained baseline from evidence that the baseline is optimal.
- Do not use held-out results to pick a runner-up or retune the objective.

The search is bounded and is not a global optimization guarantee. Error
thresholds remain fixed. Changing questions requires fresh model answers and
a separate experiment.

## 5. Apply and verify

When updating the supplied YAML is authorized, rerun the same selection with
the same immutable cases and objective, adding `--apply`. Use fresh audit and
policy output paths. The tool writes selected warning values into that YAML;
a separate override file is not a substitute.

Inspect the diff. Only intended warning reporting fields should change;
questions, error thresholds, and unrelated configuration must remain intact.
Verify the updated configuration using the same `--plan` command and rule
selection. Do not spend on a second live scan without remaining authorization.

## 6. Hand off evidence

Report the supplied rules changed or retained, exact model and dataset
identities, objective, development and held-out results, excluded/uncertain
cases, independent-support limitations, and actual cost.

Keep enough local artifacts to repeat import and selection without another
paid scan. Provide the commands used. Do not publish captured private source.
