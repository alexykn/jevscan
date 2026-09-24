# Expanded revision-2 development baseline

This is the authorized development-only capture for the final expanded
snapshot. Heldout data has not been captured or selected against.

* Snapshot:
  `omitted digest`
* Scenarios: 72 across 34 development groups.
* Requests: 72.
* Reserved input: 58,273 tokens / `$0.002447466`.
* Reported usage: 56,973 input and 2,602 output tokens; reported cost was
  `$0.002392866`.
* Cases imported: 72.
* Provisional labels used as ground truth: no.

| Rule | Cases | Labels | Confirmed statuses | Tentative statuses |
| --- | ---: | --- | --- | --- |
| JEV01 | 8 | Agree 2, Disagree 6 | ok 5, unknown 3 | warning 3 |
| JEV02 | 8 | Agree 4, Disagree 4 | ok 1, unknown 3, warning 4 | warning 1 |
| JEV03 | 8 | Agree 4, Disagree 4 | ok 4, warning 4 | none |
| JEV04 | 8 | Agree 3, Disagree 5 | ok 1, unknown 5, error 1, warning 1 | warning 1 |
| JEV05 | 8 | Agree 2, Partial 1, Disagree 5 | ok 3, unknown 5 | none |
| JEV06 | 8 | Agree 4, Disagree 4 | ok 3, unknown 5 | none |
| JEV07 | 8 | Agree 2, Disagree 6 | ok 5, unknown 1, warning 2 | none |
| JEV08 | 8 | Agree 3, Disagree 5 | ok 5, unknown 1, warning 2 | none |
| JEV09 | 8 | Agree 4, Disagree 4 | ok 3, unknown 1, warning 4 | warning 1 |

Strict cases, answers, receipts, summary, and replay remain private under
`.jevscan-calibration/`. This baseline is evidence for development policy
selection only; it is not a heldout result and does not justify default edits
without the parent selection/freeze workflow.
