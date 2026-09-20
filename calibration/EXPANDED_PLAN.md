# Expanded 108-scenario evaluation plan (superseded)

> [!WARNING]
> This revision is superseded and is not acceptance evidence. The generator is
> replacing templated JEV06/JEV07 groups G02–G06 with distinct mechanisms and
> real JEV07 ownership conflicts. Re-run normalization and both plans after the
> revision-2 snapshot and blind review are finalized.

The expanded public source snapshot was normalized into ignored manifests
without using `provisional_label` values:

```text
.jevscan-calibration/expanded-dev.yaml
.jevscan-calibration/expanded-heldout.yaml
```

Scenario IDs are prefixed `expanded:` and scenario groups are prefixed
`expanded:` so they cannot collide with the original synthetic corpus. The
manifest's fixed `partition` values map to `development` and `heldout`; paired
members remain in the same group.

Snapshot assertion:

```text
sha256:cfbae8b30d58b78ab6d8de51c9c2501e167138da17aad172a2bf33a0eb411977
```

## Offline plans

| Partition | Scenarios | Groups | Requests | Reserved input | Estimated cost |
| --- | ---: | ---: | ---: | ---: | ---: |
| Development | 72 | 36 | 72 | 54,182 tokens | `$0.002275644` |
| Heldout | 36 | 18 | 36 | 27,055 tokens | `$0.001136310` |

Each rule has 8 development scenarios and 4 heldout scenarios. All nine
packaged rules are represented. No applicability skips were produced by the
production Planner for this snapshot.

Plans are stored privately at:

```text
.jevscan-calibration/expanded-dev-plan.json
.jevscan-calibration/expanded-heldout-plan.json
```

This superseded revision was pre-capture planning only. Development capture, candidate selection,
and freeze must complete before any heldout provider call. Provisional labels
are not sent to the model and are not imported as adjudications.

## Current reviewed snapshot (planning diagnostic)

> [!WARNING]
> The source files match the reviewed aggregate hash, but the parent manifest
> metadata is still stale: it reports the old 54-group arrangement rather than
> the canonical 34 development groups. These normalized plans are reusable
> token estimates only, not acceptance evidence. Regenerate them after final
> manifest integration.

The generator's reviewed revision is now:

```text
sha256:91f1e9e84c8149f2aa08b5150d8f7d7de4d5f58cb0d6eba9e3af0820a39294aa
```

Fresh private normalized manifests and plans are:

```text
.jevscan-calibration/expanded-dev-current.yaml
.jevscan-calibration/expanded-heldout-current.yaml
.jevscan-calibration/expanded-dev-current-plan.json
.jevscan-calibration/expanded-heldout-current-plan.json
```

The current offline plans contain 72 development scenarios in 36 groups and
36 heldout scenarios in 18 groups. They estimate 72 development requests
(58,273 reserved input tokens; `$0.002447466`) and 36 heldout requests
(29,716 reserved input tokens; `$0.001240807`). No provider call has been made
for this diagnostic revision. Development capture, policy selection, and freeze must
precede any heldout capture.
