# Final built-in warning calibration plan

This plan is recorded before measuring the expanded synthetic corpus.
It fixes the selection procedure, not the desired thresholds or results.

## Evidence and independence

- Keep existing public source, labels, and measurements. They are development
  evidence, including the small synthetic corpus used to correct the objective.
- Add at least six independent scenario groups for each of the nine current
  rules. Assign four groups to development and two to held-out evaluation
  before live measurement. A repair, translation, or close derivative stays
  in its original group.
- Review expanded sources against the exact current rule without showing the
  reviewer generator labels or model answers. Preserve disagreements and
  missing-context cases as such rather than forcing binary labels.
- Verify production parsing, target selection, applicability, and source
  hashes before capture. Applicability skips are not correct negatives.
- Complete the previously labeled lookup-daemon evaluation. Preserve its
  original splits. Previously inspected real-project held-out results are
  supplementary diagnostics, not a newly untouched acceptance set.

## Selection

Use the production selector with its current default objective, candidate
budget, grouping, baseline tie preference, and positive-signal safeguard.
Use direct rule adjudications where available; retain the original label view
for sensitivity reporting rather than double-counting the same source.

Select from development data only. Record selected policy hashes before
examining expanded held-out outcomes. Apply the algorithm's selected warning
policies, including unchanged baselines when selection lacks support or finds
no improvement. Do not manually substitute a preferred threshold.

Error thresholds, questions, applicability, and rule meanings are fixed.
Rule redesign and splitting rules belong in a separate change.

## Evaluation and delivery

Compare baseline and selected confirmed, tentative, and absent outcomes by
label, rule, and corpus. Report positive misses and negative signals, not just
aggregate utility. Include independent group counts and candidate-search
truncation. Synthetic agent-written and agent-reviewed examples are useful
controlled evidence, not human ground truth or a population accuracy estimate.

There is no new hard precision or recall gate. A concrete implementation or
label-contract defect must be fixed and disclosed; reusing held-out evidence
to change the procedure makes that evidence development data and requires a
new independent check. Unexpected but valid precision/recall tradeoffs should
be reported rather than concealed through manual policy selection.

Finish by checking the installed workflow, package contents, portable skill,
privacy boundaries, and the full test/lint/type suite on the final source.

## Pre-measurement source-review corrections

The initial expansion's six groups per rule included repeated templates.
Before any provider call, repeated held-out mechanisms and several development
examples were replaced and independently reviewed again. Related JEV01
development translations were consolidated, not counted as extra support.
The resulting corpus has 108 cases in 52 groups: four groups for JEV01 and
six for each other rule, with 34 development and 18 held-out groups.
This reduces the initial numerical coverage claim without discarding cases
or changing their split. The objective and selection procedure are unchanged.
