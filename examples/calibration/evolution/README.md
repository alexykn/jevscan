# Proposed evolution-dimension development corpus

This directory is a compact public synthetic fixture set for the proposed
`unaccounted-partial-state-transition` semantic dimension. It is development
evidence only: `sources/` contains neutral code, `MANIFEST.yaml` maps one exact
target per file, and `ADJUDICATIONS.yaml` contains provisional source-only
review annotations outside the evidence root.

The exact condition is:

> Agree only when a target performs semantically coupled durable effects in
> sequence and a later failure can expose an inconsistent logical transition,
> without atomic publication, rollback, or an explicit partial/restartable
> contract. A missing helper or resource contract is Partial.

The 22 cases comprise 15 independent support groups. Translated versions share
one support group and count once as independent evidence. True defects cover
sequential coupled writes, destructive replacement, authoritative state with
required bookkeeping, and propagated errors after partial commit. Controls cover
local build plus atomic publication, temp/sync/rename, transactions,
partial-result contracts, progressive restartability, independent telemetry,
and existing JEV05/JEV07 comparison signals. Four cases intentionally omit the
helper/resource contract needed to decide atomicity or coupling.

The grouping keeps translation pairs together. T01 combines unhandled and
explicitly propagated later failures because error disposition is not a new
effect mechanism. T03 combines authoritative bookkeeping with the swallowed
failure/file-owner overlap because both test one required state-transition
invariant.

The `comparison_bucket` metadata provides `jev05_only`, `jev07_only`,
`both_overlap`, and `neither` slices for future incremental experiments. It is
metadata for experiment design, not source evidence.

Agree and Disagree cases establish their durability, coupling, atomicity,
rollback, restart, or independent-audit behavior with local implementations in
the supplied owner/file evidence. The manifest summarizes those contracts but
does not supply them; source models use neutral observable fields such as
`values`, `history`, `index`, `cursor`, and `published`. The four Partial cases
intentionally omit the helper or resource implementation that would decide the
label.

## Offline use

From the repository root:

```bash
uv run jevscan examples/calibration/evolution/sources --offline
uv run jevscan examples/calibration/evolution/sources --plan
```

These commands use the production parser and make no provider calls. The
fixtures are not a runnable application and should not be rewritten by a
formatter. The source digest in `MANIFEST.yaml` covers sorted relative paths,
NUL separators, and raw UTF-8 bytes.
