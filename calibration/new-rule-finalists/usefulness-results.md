# Final usefulness results

Status: **complete public aggregate; five promoted candidates are installed as
non-blocking JEV12–JEV16 rules**. This report records the final review of the
frozen eight-rule candidate configuration. It is not a retuning or writeback
plan.

## Method

The review used:

- eight frozen rules;
- five anonymous repository slots;
- three files per slot (candidate-rich, control, and test), for 15 files;
- one complete production scan with 1,264 judgments across 41 requests;
- blind review of all four reportable signals and a stratified sample of
  non-findings; and
- 74 reviewed cases with coverage of every rule and every slot.

The review preserved the `Agree`, `Partial`, and `Disagree` labels, evaluated
utility and review burden, and performed no retuning. The five promoted
candidates had already passed the development selector and frozen heldout.

## Rule aggregates

`A`, `P`, and `D` in the labels column mean `Agree`, `Partial`, and `Disagree`.
Signals are confirmed reportable signals; `none` is the remainder of the
reviewed cases for that rule.

| Rule | Reviewed | Groups | Labels A/P/D | Signals confirmed/none | Utility | Missed positives | False confirmed | Disposition |
| --- | ---: | ---: | --- | ---: | ---: | ---: | ---: | --- |
| `EXPOSED_MUTABLE_AUTHORITY` | 10 | 9 | 1/3/6 | 0/10 | -4 | 1 | 0 | Defer |
| `HIDDEN_CALLER_RELEVANT_EFFECT` | 9 | 6 | 0/0/9 | 0/9 | 0 | 0 | 0 | Promote low-noise |
| `HIDDEN_CALLER_RELEVANT_PREREQUISITE` | 10 | 8 | 0/0/10 | 0/10 | 0 | 0 | 0 | Promote low-noise |
| `NEW01_CONSUMER_CONFIRMED` | 7 | 5 | 2/0/5 | 0/7 | -6 | 2 | 0 | Defer |
| `NEW09_UNSAFE_RETRY` | 5 | 5 | 0/0/5 | 0/5 | 0 | 0 | 0 | Promote low-noise |
| `NEW11_FALSE_SUCCESS` | 10 | 7 | 0/4/6 | 0/10 | 0 | 0 | 0 | Promote low-noise |
| `NEW04_AMBIENT_EFFECT_A` | 13 | 8 | 1/1/11 | 4/9 | -8.4 | 1 | 4 | Reject this release |
| `NEW05_DERIVED_INCOHERENCE` | 10 | 8 | 0/2/8 | 0/10 | 0 | 0 | 0 | Promote low-noise |

The four confirmed signals for `NEW04_AMBIENT_EFFECT_A` were all labeled
`Disagree`. The result is therefore a rejection for this release, not a
threshold or contract change.

## Guidance and limitations

The five promoted candidates are **low-noise review guidance**, not blocking
findings. They are installed as `JEV12` through `JEV16` and can be disabled or
rolled back independently. The review makes no claim of real-positive recall:
recall remains unverified for every promoted candidate. The small reviewed
sample and the absence of a positive signal for the promoted candidates are
reported observations, not grounds for retuning.

Only aggregate results are published. Repository slots remain anonymous, and
private review material, locations, source content, identity digests, and local
output details are excluded.
