# Aggregate supplementary calibration result

This is an aggregate record of a bounded supplementary source review. It does
not publish external repository identities, local roots, revision identifiers,
file paths, qualified targets, line ranges, source/target hashes, manifests,
raw responses, or receipts.

## Scope

- 19 source-review targets;
- 38 imported rule cases across JEV01 and JEV02;
- 10 development targets and 9 held-out targets;
- 14 independent synthetic or external-source groups; and
- no enrichment or production-rule changes.

The raw source review and its integrity records remain local-only. They are not
required by the public synthetic replay and are not linked from the public
capture workflow.

## Aggregate outcomes

| Rule | Split | Cases | Aggregate review |
| --- | --- | ---: | --- |
| JEV01 | development | 10 | mostly clean or unresolved |
| JEV01 | held-out | 9 | clean, with unresolved boundary cases |
| JEV02 | development | 10 | clean or unresolved |
| JEV02 | held-out | 9 | a small number of source-supported review cases |

`Partial` remains a distinct unresolved category. It is not silently converted
into a clean negative or a confirmed defect. The small sample does not support
an accuracy gate, default edit, or general precision/recall claim.

## Public replay boundary

The reproducible public evidence is in the synthetic manifests, blind labels,
adjudications, captures, and evaluations. Any future source review must resolve
identity locally, retain raw records outside the repository, and publish only
an aggregate disposition without source locators or digests.
