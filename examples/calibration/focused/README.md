# Focused-question fixture corpus

This is a compact, public, source-only fixture corpus for the focused JEV01,
JEV02, and JEV04 question experiment. `sources/` is the only scanned evidence
root. `MANIFEST.yaml` contains provisional mapping annotations outside that
root; they are not human ground truth and are never provider input.

Each affected rule has six fresh held-out groups: three provisional positive
groups and three provisional negative groups. Related variants would share one
group; this corpus uses one neutral source file per group. The fixtures cover
callback and `await` boundaries, immutable captured values, cohesive lifecycle
orchestration, interleaved responsibilities, obscure and merely local control
flow, and redundant versus boundary-justified validation.

The smaller development set is represented by references to already reviewed
public expanded cases. In particular, `quarry.ts` and `summit.js` are
development-only diagnostics despite their old expanded-corpus partition; they
are not fresh held-out evidence for this experiment.

Generate ordinary custom project rules and make an offline plan from the
repository root:

```bash
uv run python calibration/focused_experiment.py config \
  --output .jevscan-calibration/focused.yaml
uv run python calibration/focused_experiment.py plan \
  --phase development \
  --config .jevscan-calibration/focused.yaml \
  --output .jevscan-calibration/focused-dev-plan.json
uv run python calibration/focused_experiment.py freeze \
  --development-plan .jevscan-calibration/focused-dev-plan.json \
  --candidate jev04-focused-joint \
  --output .jevscan-calibration/focused-freeze.json
uv run python calibration/focused_experiment.py plan \
  --phase heldout \
  --config .jevscan-calibration/focused.yaml \
  --freeze .jevscan-calibration/focused-freeze.json \
  --output .jevscan-calibration/focused-heldout-plan.json
```

The utility never sends a request and records both reserved and actual input
token fields. Captured cases are replayed through the production assessment
contract by the `metrics` command; no provider answer is asserted by corpus
tests.
