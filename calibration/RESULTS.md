# Bounded JEV01/JEV02 experiment ledger

This is an aggregate ledger only. Private manifests, source snapshots, JSONL
cases, receipts, and freeze records remain under the ignored
`.jevscan-calibration/` directory.

## Corpus and split

The live experiment used the exact frozen 33-target map:

| Repository | Development | Heldout | Total |
| --- | ---: | ---: | ---: |
| `plto` | 6 | 2 | 8 |
| `whi` | 6 | 6 | 12 |
| `iyon` | 8 | 5 | 13 |
| **Total** | **20** | **13** | **33** |

The lookup-daemon corpus and other targets from the larger prior pool are
authorized but remain pending coverage in this live run. The recovered
19-case lookup source review was collected without model answers; it requires
the same source-hash validation and fresh inference as the other repositories.
No lookup source was sent or paid for here, and no technical blocker has been
established. This 33-target experiment is a measured subset of the collected
corpus, not the complete corpus. The manifest enforced full source-hash and
owner-group separation: no source file or owner group appeared in both splits.

The private manifest identity was:

```text
manifest sha256:e39ab8caa1fbc67e4e9b73f5fd0c3840811d942f9fd6f11234756b7ed07afccc
```

## Requests and cost

* Model: `jev-1.13.0`
* Input price: `$0.042/M`; output price treated as free
* Development: 3 rounds, 57 requests, 259,286 reserved input tokens,
  195,104 reported input tokens, 7,236 reported output tokens,
  `$0.010890012` reserved and `$0.008194368` reported
* Heldout: one baseline-question capture, 10 requests, 52,033 reserved input
  tokens, 41,031 reported input tokens, 1,019 reported output tokens,
  `$0.002185386` reserved and `$0.001723302` reported
* Cumulative: 67 requests, 311,319 reserved input tokens,
  `$0.013075398` reserved and `$0.009917670` reported

All request receipts record body/evidence hashes, reservations, reported
usage, and costs in the ignored private ledger. No API key or request body was
stored.

## Development candidates

Development evaluated four bundles over identical evidence:

1. `baseline`: packaged JEV01 and JEV02 questions.
2. `jev01-balanced-a`: target-itself/policy-state JEV01 wording.
3. `jev01-balanced-b`: target-itself/interleaving/material-harm JEV01 wording.
4. `jev02-clarified`: packaged JEV01 plus the explicit level-1 traceability
   clarification.

The two balanced JEV01 variants produced zero signal on the only development
`Agree` target, so they were rejected. The clarified JEV02 wording lowered
development high-level mass recall and was rejected. No packaged rule or
default threshold was edited.

The reporting-only development sweep measured, but did not install:

* JEV01 warning probability `0.6`, preserving the sole development and
  heldout positives while removing one known heldout tentative `Disagree`
  signal.
* JEV02 warning mass on levels `[2, 3]` at probability `0.6` with the
  existing confidence gate, and conservative level-3 error mass at
  probability/confidence `0.8`.

The frozen alternative policy identity was:

```text
sha256:113a96985d851c4dbfae4a4a0bbe60b2fe7a4f8e60f829aea9e42d6c0886a336
```

Development baseline versus measured reporting alternative counts were:

| Rule/policy | Confirmed | Tentative | Positive/level denominators |
| --- | ---: | ---: | --- |
| JEV01 baseline | 7 | 3 | Agree `1`: 1/1 confirmed; Disagree `19`: 6/19 confirmed, 3 tentative |
| JEV01 measured `.6` | 7 | 0 | Agree `1`: 1/1 confirmed; Disagree `19`: 6/19 confirmed |
| JEV02 baseline | 5 | 9 | level 1 `17`: 3 confirmed, 8 tentative; levels 2+3 `3`: 2 confirmed, 1 tentative |
| JEV02 measured mass | 4 | 0 | level 1 `17`: 2 confirmed; levels 2+3 `3`: 2 confirmed |

## Heldout comparison

The heldout request used the baseline questions once. Baseline and the
reporting-only alternative were replayed from the same 26 answers.

| Rule/policy | Confirmed | Tentative | Confirmed recall | Review-list recall |
| --- | ---: | ---: | ---: | ---: |
| JEV01 baseline | 7 | 1 | Agree 4/4; Partial 1/1 | Agree 4/4; Partial 1/1 |
| JEV01 measured `.6` | 7 | 0 | Agree 4/4; Partial 1/1 | Agree 4/4; Partial 1/1 |
| JEV02 baseline | 4 | 8 | levels 2+3: 4/11 | levels 2+3: 11/11 |
| JEV02 measured mass | 4 | 4 | levels 2+3: 4/11 | levels 2+3: 8/11 |

For JEV01, both policies confirmed `2/8` heldout `Disagree` targets; the
measured `.6` policy removed one tentative `Disagree` signal. For JEV02, the
measured policy removed one level-1 tentative signal but also removed three
tentative signals on expected levels 2+3. The current JEV02 defaults therefore
remain preferable for now. These small, selected sets do not justify an
accuracy gate or a default edit.

Heldout raw expected JEV02 levels were level 1: `2`, level 2: `7`, and level
3: `4`. The levels-2+3 probability-mass diagnostic had mean `0.587692`,
precision `1.0`, and recall `9/11 = 0.81818` at its diagnostic `0.5` mass
threshold. Provider scores are continuous; nearest-level and argmax
comparisons are recorded separately in the private selection report and are
not treated as the score truth.

## Reproduction

See [`EXPERIMENTS.md`](EXPERIMENTS.md) for validation, three-round development
preflight/live capture, reporting-only selection, freeze, and one-call
heldout replay commands. The public code does not contain private source or
labels.
