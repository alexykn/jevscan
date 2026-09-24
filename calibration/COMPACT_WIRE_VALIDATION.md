# Compact question and shared-rubric validation

This is a bounded evaluation of the proposed prompt-v6 wire format, not proof
that its answers equal prompt v5. All 16 built-in rules stay enabled. JEV01 and
JEV02 retain their original question wording: a source-matched comparison found
that shortening them reduced useful positive signals. JEV03–JEV16 use shorter
questions, and the wire sends each selected rubric once per request instead of
once per target. The shared policy, target locator, and rubric reference are
part of the new prompt identity. The model was pinned to `jev-1.13.0`.

## Cost and coverage

| Measurement | Result |
| --- | ---: |
| Offline initial plan on the 42-file regression project, merged defaults | 7,850 checks; 180 requests; 6.31M estimated input tokens; $0.265 reserved |
| Offline initial plan on the same checkout, proposed defaults | 7,850 checks; 125 requests; 1.81M estimated input tokens; $0.076 reserved |
| Completed uncached three-file real-project scan, enrichment enabled | 995/995 checks; 22 requests; 8 enrichment calls; $0.0115 provider-reported, $0.0113 reserved; 4.7 seconds |
| Reviewed public synthetic JEV01–09 cases | 108 imported; $0.0162 provider-reported for the final compatible captures |
| Reviewed public synthetic JEV10 cases | 20 imported; two negative controls excluded by applicability; $0.0030 provider-reported |

The initial plan excludes enrichment, recovery, retries, and cache effects. The
42-file cold plan is **not below $0.05**; no complete 42-file live run was made
with the proposed defaults. The earlier incomplete run's reported and reserved
costs must not be compared directly with these cold-plan estimates. Source-
containing captures, reviews, and cost receipts remain in ignored local storage;
the public figures above do not include those artifacts. Additional exploratory
prompt comparisons incurred small charges not included in the final-capture
cost rows.

## Source-reviewed outcomes

The existing predeclared [agent-review objective](agent-review-objective.yaml)
was used for development-only warning selection. Synthetic groups, translation
pairs, and their development/held-out splits were retained. JEV01–02 wording
was restored **after** observing results on these sources, including held-out
results. Those cases are no longer blind for question selection: the figures
below are descriptive diagnostics, not independent proof of the final prompts.
The JEV01–09 corpus has eight development and four nominal held-out judgments
per rule; denominators are very small:

| Rule | Held-out confirmed Agree | Held-out review-list Agree | Held-out confirmed Disagree |
| --- | ---: | ---: | ---: |
| JEV01 | 0/1 | 0/1 | 0/3 |
| JEV02 | 0/2 | 2/2 | 0/2 |
| JEV03 | 1/2 | 2/2 | 0/2 |
| JEV04 | 0/1 | 0/1 | 1/3 |
| JEV05 | 0/1 | 0/1 | 0/3 |
| JEV06 | 0/1 | 0/1 | 0/3 |
| JEV07 | 0/1 | 0/1 | 1/3 |
| JEV08 | 0/1 | 1/1 | 0/2 |
| JEV09 | 2/2 | 2/2 | 0/2 |

These are the selector's held-out outcomes for its development-selected
candidate, which can equal the current warning policy. The selector proposed
changes to JEV02 and JEV03, but JEV02 still had no confirmed held-out Agree
cases. No threshold candidate was applied: the evidence does not establish
better production behavior. JEV10 had 20 development judgments only (nine
Agree, seven Disagree, four Partial): six confirmed Agree, one confirmed
Disagree, and four uncertain Partial with its current reporting policy. Its
development-only candidate was not applied without independent held-out data.

JEV11–JEV16 have no fresh independent source-reviewed positive/negative
calibration set for prompt v6. The real-project sample establishes completion,
cost, and absence of confirmed findings in those files, **not positive recall**.
Historical v4/v5 answer aggregates cannot be used as v6 answers. Existing
warning and error thresholds remain unchanged.

The final JEV01–09 capture combines two source-matched runs because JEV01–02
wording was restored after the first pass. Before joining the 24 and 84 cases,
each case's full rule contract and shared registry were checked against the
final packaged defaults; raw source, labels, groups, and splits were not
changed. Reviewed labels from the original normalized manifest were matched
to the source manifest by exact source hash, rule, split, and group. The
development-only threshold selector did not use held-out labels, and no
threshold candidate was applied. A future independent source group is needed
to validate the final wording without this reuse.
