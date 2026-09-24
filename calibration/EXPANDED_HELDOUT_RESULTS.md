# Expanded revision-2 heldout baseline

This report records the authorized heldout baseline capture. The frozen policy
was established before this call; no frozen thresholds were applied during
capture, and no further calls were made.

* Snapshot:
  `omitted digest`
* Scenarios: 36 across 18 heldout groups.
* Requests: 36.
* Reserved: `$0.001248072`.
* Reported: `$0.001211322`.
* Imported strict cases: 36.
* Provisional labels used as ground truth: no.

| Rule | Cases | Labels | Confirmed statuses | Tentative statuses |
| --- | ---: | --- | --- | --- |
| JEV01 | 4 | Agree 1, Disagree 3 | ok 2, warning 2 | none |
| JEV02 | 4 | Agree 2, Disagree 2 | unknown 3, warning 1 | warning 1 |
| JEV03 | 4 | Agree 2, Disagree 2 | ok 2, unknown 1, warning 1 | none |
| JEV04 | 4 | Agree 1, Disagree 3 | unknown 4 | warning 1 |
| JEV05 | 4 | Agree 1, Disagree 3 | ok 2, unknown 2 | none |
| JEV06 | 4 | Agree 1, Disagree 3 | unknown 4 | none |
| JEV07 | 4 | Agree 1, Disagree 3 | ok 2, warning 2 | none |
| JEV08 | 4 | Agree 1, Disagree 2, Partial 1 | ok 2, unknown 2 | warning 1 |
| JEV09 | 4 | Agree 2, Disagree 2 | ok 2, warning 2 | none |

The strict cases, answers, receipts, summary, and replay remain private under
`.jevscan-calibration/`. This is a baseline replay for the parent’s frozen
policy comparison, not a selector decision or default-rule edit.
