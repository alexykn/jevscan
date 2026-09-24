# Baseline-only JEV01/JEV02 assembly

The earlier JEV01/JEV02 development capture contains four question bundles.
Only records whose provenance has `candidate: baseline` belong in the canonical
baseline comparison. Do not combine balanced-JEV01 or clarified-JEV02 records
with baseline records.

The private assembled artifact is:

```text
.jevscan-calibration/baseline-cases.jsonl
```

It contains:

* 40 development cases: JEV01/JEV02 across 20 dev targets;
* 26 heldout cases: JEV01/JEV02 across 13 heldout targets;
* 66 total cases with no duplicate case IDs;
* `provenance.source_group`, populated from the preserved `owner_group`.

The assembly preserves the original synthetic case identity and review
boundaries without publishing machine-local source identity or digests. Any
source-integrity records needed for a private replay remain in local-only
storage. The replay artifact is:

```text
.jevscan-calibration/baseline-replay.json
```
