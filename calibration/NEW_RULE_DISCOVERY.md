NEW RULES ACTUALLY LIVE-TESTED: 15

# Final new-rule discovery report

Status: complete public aggregate. Ten original `NEWxx` candidates and five
recovered candidates were live-tested. The current production range is
`JEV01`–`JEV16`. The original wave installed `JEV11`; the final usefulness
review installed five additional candidates as non-blocking `JEV12`–`JEV16`
guidance.

## Infrastructure reused

The work reused the production assessment path rather than introducing a
second scorer:

- Tree-sitter extraction, planning, context assembly, target selection,
  request packing, and bounded token reservations;
- the normal rule, check, import, replay, and assessment contracts;
- shared-state request batching, completion accounting, and no-retry budgets;
- development-only policy selection followed by a fresh held-out run with no
  held-out tuning, writeback, or relabeling; and
- separate `Agree`, `Partial`, and `Disagree` judgments, with `Partial` never
  converted into a positive.

The measured waves disabled cache and enrichment. The public report contains
aggregates only. Private source review, raw captures, raw responses, and
source-identifying metadata are excluded and remain local; no raw files are
committed.

## Anonymous source-derived principles

The reviewed principles are generic boundaries, not claims about any named
repository:

1. **Lifecycle state:** require a visible terminal marker, a reachable
   stateful effect, and no rejection or proven restart, reset,
   reinitialization, or fresh generation. Idempotent close, draining,
   diagnostics, per-operation terminality, and valid generations are
   controls. An absent external contract remains uncertain.
2. **Representation and publication:** require one authority, a reachable
   mutation, and a visible consumer consequence. Fresh snapshots, bounded
   staleness, progressive protocols, atomic publication, metrics, and
   telemetry are not defects when their contract is visible.
3. **Coordination and completion:** require overlap on the same logical
   resource plus a concrete lost update, duplicate effect, or duplicate work.
   A success claim also needs a visible required postcondition and a reachable
   omission. Unknown transitive behavior does not prove a finding.
4. **Caller-facing contracts:** hidden effects, prerequisites, and outcome
   partitions require visible caller burden. Names, comments, tests, generic
   types, and ordinary conventions are not proof.

## Coverage of JEV01–JEV11

| Rule | Covered dimension | Boundary preserved |
| --- | --- | --- |
| JEV01 | Mixed responsibilities | Not a lifecycle-state claim |
| JEV02 | Unclear control flow | Structural traceability, not terminal reentry |
| JEV03 | Mixed abstraction levels | Abstraction boundary, not lifecycle correctness |
| JEV04 | Redundant validation | Validation ownership, not terminal reentry |
| JEV05 | Hidden invariant failure | Failure-path handling, not every state defect |
| JEV06 | Unhelpful decomposition | Helper usefulness, not hidden caller burden |
| JEV07 | Fragmented ownership | Authority ownership, not every alias or projection |
| JEV08 | Incohesive owner | Cohesion, not state-machine reentry |
| JEV09 | Duplicated behavior | Duplication, not every repeated representation |
| JEV10 | Unaccounted partial state transition | Coupled temporal effects, not every completion claim |
| JEV11 | Terminal-state reentry | Terminal operation without visible restart or reuse contract |

## Candidate inventory and aggregate dispositions

The inventory has 15 candidates. Development values are aggregate labels or
exact-choice counts; they are not per-case data.

| Candidate | Wave and development result | Fresh held-out result | Final usefulness result | Disposition |
| --- | --- | --- | --- | --- |
| `NEW01` representation loss / consumer distinction | Original; no acceptable selected signal | Not selected | `NEW01_CONSUMER_CONFIRMED`: defer, utility `-6`, 2 missed positives | Defer |
| `NEW02` coupled invalid-state escape | Original; no acceptable selected signal | Not selected | Not reviewed | Research only |
| `NEW04` hidden ambient effect | Original; some separation, insufficient recall | Not selected | `NEW04_AMBIENT_EFFECT_A`: reject this release, utility `-8.4`, 4 false confirmed, 1 missed positive | Reject this release |
| `NEW05` stale derived representation | Original; selected, development support | Agree case/group `2/3`, confirmed `1/3` | Promote low-noise, utility `0` | Production `JEV14` non-blocking |
| `NEW06` mixed-version decision | Original; no positive recall and one false signal | Not selected | Not reviewed | Research only |
| `NEW07` incomplete aggregate exposure | Original; no acceptable selected signal | Not selected | Not reviewed | Research only |
| `NEW08` terminal-state reentry | Original; selected | Agree case/group `3/3`, confirmed `3/3`, no false review | Installed as `JEV11` | Production `JEV11` |
| `NEW09` unsafe retry | Original; insufficient labeled support | Not selected | Promote low-noise, utility `0` | Production `JEV15` non-blocking |
| `NEW10` uncoordinated same-key operation | Original; selected | Agree case/group `6/6`, confirmed `6/6`, no false review | Not reviewed | Not promoted; false-review workload in bounded external review |
| `NEW11` success before required result | Original; selected | Agree case/group `3/3`, confirmed `3/3`, 2 false reviews | Promote low-noise, utility `0` | Production `JEV16` non-blocking |
| `EXPOSED_MUTABLE_AUTHORITY` | Recovered; exact choice `22/35` | Not run | Defer, utility `-4`, 1 missed positive | Deferred |
| `SPLIT_OPERATIONAL_POLICY` | Recovered; exact choice `44/50`; follow-up stopped at its gate | Not run | Not reviewed | Deferred for refinement |
| `IMPLICIT_OUTCOME_PARTITIONS` | Recovered; exact choice `28/40` | Not run | Excluded from final usefulness set | Deferred for refinement |
| `HIDDEN_CALLER_RELEVANT_EFFECT` | Recovered; exact choice `38/60` | Not run separately | Promote low-noise, utility `0` | Production `JEV12` non-blocking |
| `HIDDEN_CALLER_RELEVANT_PREREQUISITE` | Recovered; exact choice `53/70` | Not run separately | Promote low-noise, utility `0` | Production `JEV13` non-blocking |

### Original development and fresh held-out waves

The original development wave covered 10 candidates and 930 judgments. It
selected only `NEW08` for a production proposal; `NEW05`, `NEW09`, `NEW10`,
and `NEW11` remained evidence-gated at that stage, while the other candidates
stayed research-only. The later usefulness review installed the five
low-noise candidates as non-blocking rules.

The fresh held-out wave covered the four finalist survivors `NEW05`, `NEW08`,
`NEW10`, and `NEW11` across 116 judgments. `NEW08` was clean and supported
`JEV11`. `NEW05` lost recall and confirmed recall. `NEW10` and `NEW11`
retained synthetic recall, but their bounded external review did not justify
blocking promotion. The final usefulness review subsequently installed the
five promoted candidates as non-blocking rules.

### Recovered development wave

The recovered wave covered five corrected candidates across 255 judgments and
51 independent semantic groups. That wave was development-only: exact-choice
accuracy or clean controls alone did not authorize production numbering. The
finalist usefulness review supplied the later held-out evidence used for the
five non-blocking production mappings; no separate recovered-only held-out run
was performed.

### Final usefulness review

The final usefulness aggregate is authoritative for its frozen eight-candidate
set. It covered five anonymous repository slots, three files per slot, 1,264
judgments, 41 requests, 74 reviewed cases, all four reportable signals, and a
stratified sample of 70 non-findings. No retuning occurred.

| Final candidate | Labels A/P/D | Confirmed signals | Utility | Missed positives | False confirmed | Aggregate disposition |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| `EXPOSED_MUTABLE_AUTHORITY` | `1/3/6` | 0 | -4 | 1 | 0 | Defer |
| `HIDDEN_CALLER_RELEVANT_EFFECT` | `0/0/9` | 0 | 0 | 0 | 0 | Install `JEV12` non-blocking |
| `HIDDEN_CALLER_RELEVANT_PREREQUISITE` | `0/0/10` | 0 | 0 | 0 | 0 | Install `JEV13` non-blocking |
| `NEW01_CONSUMER_CONFIRMED` | `2/0/5` | 0 | -6 | 2 | 0 | Defer |
| `NEW09_UNSAFE_RETRY` | `0/0/5` | 0 | 0 | 0 | 0 | Install `JEV15` non-blocking |
| `NEW11_FALSE_SUCCESS` | `0/4/6` | 0 | 0 | 0 | 0 | Install `JEV16` non-blocking |
| `NEW04_AMBIENT_EFFECT_A` | `1/1/11` | 4 | -8.4 | 1 | 4 | Reject this release |
| `NEW05_DERIVED_INCOHERENCE` | `0/2/8` | 0 | 0 | 0 | 0 | Install `JEV14` non-blocking |

The four confirmed usefulness signals were all `Disagree`. The five
low-noise promotions are review guidance, not blocking findings; each is
independently disableable and rollbackable as `JEV12`–`JEV16`. Real-positive
recall remains unverified for every one.

## Production mapping

`JEV11` is installed for the terminal-state rule from the original wave. The
following five low-noise usefulness candidates are installed as non-blocking
production rules:

| Installed rule | Candidate | Status |
| --- | --- | --- |
| JEV12 | `HIDDEN_CALLER_RELEVANT_EFFECT` | Installed non-blocking guidance |
| JEV13 | `HIDDEN_CALLER_RELEVANT_PREREQUISITE` | Installed non-blocking guidance |
| JEV14 | `NEW05_DERIVED_INCOHERENCE` | Installed non-blocking guidance |
| JEV15 | `NEW09_UNSAFE_RETRY` | Installed non-blocking guidance |
| JEV16 | `NEW11_FALSE_SUCCESS` | Installed non-blocking guidance |

These rules are enabled by the packaged defaults but are non-blocking review
guidance: `report.blocks_exit: false` excludes their findings from `--fail-on`
while preserving their reported severity and thresholds. They remain
independently disableable and rollbackable.

## Deferred and rejected candidates

- **Deferred for more evidence:** `EXPOSED_MUTABLE_AUTHORITY`,
  `NEW01_CONSUMER_CONFIRMED`, `NEW02`, `NEW06`, `NEW07`,
  `SPLIT_OPERATIONAL_POLICY`, and `IMPLICIT_OUTCOME_PARTITIONS`.
- **Rejected for this release:** `NEW04_AMBIENT_EFFECT_A` because all four
  confirmed usefulness signals were `Disagree`; `NEW10` because the bounded
  external review produced unacceptable false-review workload. The five
  installed rules are non-blocking guidance and are not rejected or deferred
  by this final usefulness decision.
- **Screened out or merged:** cleanup/ownership-gap ideas belong to static
  analysis; caller-knowledge leakage is retained only as a narrow boundary
  inside outcome-partition work; semantic projection fanout remains research
  only; policy-authority leakage is not a stable generic source-only claim.

## Aggregate cost

The cumulative ledger covers the calibration waves, including incomplete work
and one failed attempt. It records:

- 383 accepted requests and 384 total attempts;
- 4,702,289 reported input tokens and 466,003 reported output tokens;
- reported cost **$0.197497648**;
- reserved cost **$0.282991050**; and
- a conservative authorized commitment of **$0.422991050**, including the
  failed-attempt cap, against a **$1.00** task budget, leaving **$0.577008950**.

The cost aggregate is not a production usage estimate. Conclusions use the
completed aggregate usefulness review and the evidence-gated synthetic waves,
not incomplete or private local review material.

## Caveats and privacy boundary

- Samples are small, correlated, and not a representative estimate of
  real-project precision or recall.
- A missing contract remains uncertain; thresholds cannot repair a candidate
  whose positive and negative cases are misordered.
- The usefulness review found no confirmed positive for any low-noise
  promotion, so real-positive recall is unverified.
- The recovered-only development wave had no separate fresh held-out
  evaluation; the finalist set received a fresh held-out evaluation before
  the final usefulness review.
- Private repository identities, local paths, source text, source locators,
  target identities, revision identifiers, digests, raw captures, and raw
  responses are excluded from this public report and remain local.
