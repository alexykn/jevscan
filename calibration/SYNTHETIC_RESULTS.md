# All-builtins synthetic baseline ledger

This aggregate is a compact record of the first all-nine-rule baseline capture.
The public source corpus and manifest are the input; unlabeled answers,
receipts, and strict cases remain in the ignored `.jevscan-calibration/`
directory rather than being copied into this report.

## Capture identity

* Snapshot assertion:
  `sha256:6393505d40cd945c81a27481ebbbda894f19338cef864f7ce138b748ec11568b`
* Model: `jev-1.13.0`
* Manifest scenarios: 32
* Applicable provider answers: 29
* Production applicability skips: 3
  (`JEV04` validation candidate, `JEV05` fallback candidate, `JEV06`
  helper relationship)
* Scenario groups: 20
* Requests: 29
* Reserved input: 22,324 tokens / `$0.000937608`
* Reported usage: 22,041 input and 10,888 output tokens / `$0.000925722`
* Finalized adjudications imported: 29
* Provisional manifest labels used as ground truth: **no**

The capture is an initial query/evidence baseline, not a full scan result:
semantic enrichment was not run, and no selector or default rule was changed.
Paired or translated scenarios remain in their source groups. Leave-one-group-out
sensitivity is supportable only for rules with at least two independent groups;
JEV02, JEV03, and JEV07 have one group in this corpus.

## Per-rule outcomes

`exact` is answer-contract accuracy, not reporting utility. Boolean Noul
thresholds and continuous score nearest/argmax levels are diagnostics only.
`confirmed` and `review` are production assessment signals, evaluated
separately from exact answers. `Partial` and `insufficient_context` remain
uncertain categories; they are not coerced into clean negative examples.

| Rule | Cases | Labels | Exact diagnostic | Defect/reporting result |
| --- | ---: | --- | --- | --- |
| JEV01 | 4 | Agree 1, Partial 2, Disagree 1 | Noul threshold 2/2 among adjudicated booleans | Positive 0 confirmed, 1 review/1; ambiguous cases remain Partial |
| JEV02 | 2 | Agree 1, Disagree 1 | Nearest 1/2; argmax 1/2 | Expected level 2+ positive 1 confirmed and 1 review/1 |
| JEV03 | 2 | Agree 1, Disagree 1 | Noul threshold 2/2 | Positive 0 confirmed, 1 review/1 |
| JEV04 | 3 | Agree 1, Disagree 2 | Choice 2/3 | Defect 1 confirmed and 1 review/1 |
| JEV05 | 7 | Agree 2, Partial 2, Disagree 3 | Choice 7/7; `insufficient_context` 2/2 | Defect 2 confirmed and 2 review/2 |
| JEV06 | 3 | Agree 2, Disagree 1 | Choice 2/3 | Defect 1 confirmed and 1 review/2 |
| JEV07 | 2 | Agree 1, Disagree 1 | Noul threshold 2/2 | Positive 0 confirmed, 1 review/1 |
| JEV08 | 3 | Agree 1, Disagree 2 | Noul threshold 3/3 | Positive 1 confirmed and 1 review/1 |
| JEV09 | 3 | Agree 1, Disagree 2 | Noul threshold 3/3 | Positive 1 confirmed and 1 review/1 |

The full strict cases are locally available at
`.jevscan-calibration/synthetic-cases.jsonl` and replay successfully through
`jevscan-calibrate`. They retain source/evidence hashes, scenario groups,
adjudication reasons, expected answers, applicability metadata, and returned
model identity. They are intentionally not committed by default.
