# Lookup-daemon JEV01/JEV02 baseline

This is a bounded supplementary capture from the authorized lookup-daemon
source review. It is not an all-rule calibration set and does not claim
coverage for JEV03–JEV09.

## Identity and capture

* Source-only review targets: 19.
* Exact source roots, commits, parser-qualified symbols, and review target
  hashes validated: `19/19`.
* Imported strict cases: 38 (`JEV01` and `JEV02` for each target).
* Splits: 10 development targets / 9 heldout targets, with owner groups kept
  intact.
* Independent source groups: 14.
* Model: `jev-1.13.0`.
* Requests: 14.
* Reserved input: 55,625 tokens / `$0.002336250`.
* Reported usage: 43,045 input and 1,719 output tokens /
  `$0.001807890`.

The capture used the existing rule questions and initial production
Planner/ContextBuilder evidence. It did not run enrichment or alter rules.
The complete raw capture, receipts, pending manifest, adjudication metadata,
strict cases, and replay report remain under ignored `.jevscan-calibration/`.
The cases preserve the reviewed source commits and target hashes as their
source identity. They are not rewritten to a later repository `HEAD` summary.
The capture manifest hash identifies the local pending metadata; the
per-target source hashes remain the authoritative content identities.

## Replay outcomes

| Rule | Split | Cases | Labels | Confirmed statuses | Tentative statuses |
| --- | --- | ---: | --- | --- | --- |
| JEV01 | dev | 10 | Disagree 10 | ok 9, unknown 1 | none |
| JEV01 | heldout | 9 | Disagree 7, Partial 2 | ok 7, unknown 2 | warning 2 |
| JEV02 | dev | 10 | Disagree 9, Partial 1 | ok 3, unknown 7 | warning 2 |
| JEV02 | heldout | 9 | Agree 3, Disagree 6 | ok 2, unknown 7 | warning 3 |

`Partial` preserves the original unresolved review category. It is not
converted into a clean negative or a confirmed defect.

The three heldout JEV02 `Agree` cases produced `0` confirmed findings, `2`
tentative warnings, and `1` clean/no-signal result. The aggregate confirmed
and tentative counts above include all heldout labels.

## Reproduction

The ignored lookup manifest is a local research adapter input, not the
published synthetic manifest schema. `calibration/capture.py` recognizes its
`status: pending_source_validation_and_fresh_inference` marker and expands
each target's `review_judgments` into schema-1 scenarios. Targets supply the
authorized root, relative path, qualified name, owner group, split, review
line range, and line-slice target hash. This adapter is specific to the
preserved Python lookup review; it is not part of the installed calibration
commands or a general-purpose project manifest format.

```console
uv run python calibration/capture.py plan \
  --manifest .jevscan-calibration/lookup-pending-manifest.json
uv run python calibration/capture.py capture \
  --manifest .jevscan-calibration/lookup-pending-manifest.json \
  --output .jevscan-calibration/lookup-answers.jsonl \
  --receipts .jevscan-calibration/lookup-receipts.jsonl \
  --live --max-cost 0.05
uv run python calibration/capture.py import \
  --manifest .jevscan-calibration/lookup-pending-manifest.json \
  --answers .jevscan-calibration/lookup-answers.jsonl \
  --adjudications .jevscan-calibration/lookup-adjudications.json \
  --output .jevscan-calibration/lookup-cases.jsonl
uv run jevscan-calibrate .jevscan-calibration/lookup-cases.jsonl \
  --output .jevscan-calibration/lookup-replay.json
```
