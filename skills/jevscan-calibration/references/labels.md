# Source-review labels

The selector interprets labels as truth of a rule's reportable defect claim.
They are not accuracy labels for the model's chosen answer:

| Label | Meaning |
| --- | --- |
| `Agree` | The exact defect claim is supported and deserves to be surfaced. |
| `Disagree` | The exact defect claim is unsupported or not warning-worthy. |
| `Partial` | The evidence is unresolved, the claim overstates a related concern, or its fit is genuinely ambiguous. |

A clean case with a correct model answer is still `Disagree` with the defect
claim. A missed defect is still `Agree`. Otherwise, reviewing non-findings
would reverse the training signal.

Examples:

- A responsibilities rule does not become correct merely because the function
  has complicated control flow. Label the stated responsibility claim.
- A rubric level explicitly described as easy to trace is not a positive
  control-flow defect just because the current threshold emits a warning.
- A fallback cannot be called an invariant violation without evidence of the
  invariant. Missing contracts can justify `Partial` or the rule's explicit
  insufficient-context category.
- A syntax applicability skip is not a negative model judgment. Keep skips
  separate from labeled evaluated cases.

The default objective treats Partial separately: confirmed Partial findings
are penalized, tentative Partial findings receive credit, and omitted Partial
cases are neutral. Do not silently turn Partial into half an Agree.

## Label document

Use the captured `case_id` verbatim. A typical YAML document is:

```yaml
cases:
  - case_id: COPY_EXACT_CAPTURED_CASE_ID
    label: Agree
    split: development
    source_group: project/component/owner
    explanation: >
      The visible operation establishes the invariant, then the target repeats
      that same check without mutation or a new boundary.
```

Every evaluated captured case required by the importer needs one label.
Do not create labels for applicability or omission records. The importer
validates the one-to-one relationship; do not drop inconvenient cases to make
metrics look better.

`source_group` identifies dependent examples, not individual files by default.
Use the same group for versions, corrected counterparts, translations, and
closely related targets. Include repository identity to avoid accidental
cross-project group collisions.

Do not infer warning/error severity from model confidence. Optional
`adjudicated_severity` is for separately justified severity evaluation, not
required warning-threshold selection.
