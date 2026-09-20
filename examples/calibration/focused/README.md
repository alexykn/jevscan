# Focused-question fixture corpus

This is a compact, public, source-only fixture corpus for the focused JEV01,
JEV02, and JEV04 question experiment. `sources/` is the only scanned evidence
root. `MANIFEST.yaml` contains provisional mapping annotations outside that
root; they are not human ground truth and are never provider input.

Each affected rule has six fresh held-out groups. JEV01 and JEV04 have three
provisional positive and three provisional negative groups; JEV02 has two
reviewed positive groups (expected levels 3 and 2) and four negative groups
(levels 1, 0, 0, and 0). Related variants would share one group; this corpus
uses one neutral source file per group. The fixtures cover
callback and `await` boundaries, immutable captured values, cohesive lifecycle
orchestration, interleaved responsibilities, obscure and merely local control
flow, and redundant versus boundary-justified validation.

Development imports every independently adjudicated JEV01, JEV02, and JEV04
case from `calibration/expanded-cases.jsonl`, including the reviewed
`quarry.ts` and `summit.js` snapshots. All imported cases are development-only
metadata; their stored answers are never reused for focused candidate variants.
Additional documents that are not already part of production evidence are
rejected rather than recorded as if supplied.

Generate ordinary custom project rules and make an offline plan from the
repository root:

```bash
uv run python calibration/focused_experiment.py config \
  --output .jevscan-calibration/focused.yaml
uv run python calibration/focused_experiment.py plan \
  --phase development \
  --config .jevscan-calibration/focused.yaml \
  --candidate jev04-pair-joint \
  --output .jevscan-calibration/focused-dev-plan.json
uv run python calibration/focused_experiment.py freeze \
  --development-plan .jevscan-calibration/focused-dev-plan.json \
  --candidate jev04-pair-joint \
  --output .jevscan-calibration/focused-freeze.json
uv run python calibration/focused_experiment.py plan \
  --phase heldout \
  --config .jevscan-calibration/focused.yaml \
  --candidate jev04-pair-joint \
  --freeze .jevscan-calibration/focused-freeze.json \
  --output .jevscan-calibration/focused-heldout-plan.json
```

Planning and replay are offline. A live capture is explicit, requires
`TYPESAFE_API_KEY`, and is pinned to `jev-1.13.0`:

```bash
uv run python calibration/focused_experiment.py capture \
  --phase development \
  --config .jevscan-calibration/focused.yaml \
  --candidate jev04-pair-joint \
  --output .jevscan-calibration/focused-dev.jsonl \
  --ledger .jevscan-calibration/focused-ledger.json
```

The capture reserves every request and retry plus a 25% localization/follow-up
margin against the cumulative USD 0.10 cap before dispatch. Repeated
invocations use the same ledger. It records planned estimates, body
reservations, provider usage when reported, model and identity hashes, and
client attempt counters. Tests use mock transports only and never contact a
provider. Captured cases are replayed through the production assessment
contract by the `metrics` command. Pair questions are purchased only for
exactly one bounded extracted pair; all other selected pair parents are
explicit whole-target fallbacks. Candidate-level pair metrics use captured
`jev04-baseline` records for those fallback parents, while pair-decomposed
children are composed only as a conservative diagnostic AND after their pair
metadata matches.
