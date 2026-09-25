"""Pure enrichment admission and routing policy.

No source retrieval or provider transport lives here. This contract is shared
by live enrichment and offline calibration-capture validation.
"""

from dataclasses import dataclass

from jevscan.core.assessment import Assessment
from jevscan.core.config import EnrichmentConfig
from jevscan.core.protocol import Answer, Check, ChoiceAnswer, NoulAnswer
from jevscan.core.rules import ChoiceQuestion, EnrichmentTrigger, NoulQuestion, Question

# Code owns admission and ordering; Jev only chooses useful evidence after admission.
REVIEW_PRIORITY: dict[EnrichmentTrigger, int] = {
    "missing_evidence": 0,
    "reduced_context": 1,
    "applicability": 2,
    "low_confidence": 3,
    "low_choice_probability": 3,
    "weak_defect_signal": 3,
    "probability_ambiguous": 4,
}


def review_trigger(check: Check, decision: Assessment, context_complete: bool) -> EnrichmentTrigger | None:
    """Admit actionable uncertainty, prioritizing evidence gaps even when confidence is also low."""
    if decision.status != "unknown" or not check.rule.enrich:
        return None
    reasons = {decision.reason}
    if not context_complete:
        reasons.add("reduced_context")
    if check.rule.report.not_applicable_choices:
        reasons.add("applicability")
    return next((reason for reason in REVIEW_PRIORITY if reason in reasons and reason in check.rule.enrich_on), None)


DISPOSITIONS = {
    "not_applicable": "The rule does not apply to this target's operation, regardless of missing context.",
    "sufficient": "The rule applies and the supplied evidence suffices; remaining uncertainty is interpretation, not a missing fact.",
    "local_evidence": "The rule applies and additional local source could supply a concrete missing fact relevant to the judgment.",
    "unavailable": "The rule applies but the essential missing fact is external or runtime-only; local source is unlikely to establish it.",
}
EVIDENCE_FAMILIES = {
    "callers": "Actual uses of the target that constrain its inputs, results or lifecycle.",
    "definitions": "Referenced implementations or type/contract definitions, including related Rust impls.",
    "tests": "Concrete tests that clarify intended behavior, but do not prove universal guarantees.",
    "enclosing_context": "The surrounding owner or file implementation, when not already supplied.",
}


@dataclass(frozen=True, slots=True)
class Routing:
    disposition: str | None
    families: tuple[str, ...] = ()


def allowed_families(check: Check, limits: EnrichmentConfig) -> tuple[str, ...]:
    if not limits.enabled or limits.mode == "off":
        return ()
    if limits.mode == "full":
        return tuple(EVIDENCE_FAMILIES)
    aliases = {"callees": "definitions"}
    return tuple(
        dict.fromkeys(
            aliases.get(configured, configured)
            for configured in check.rule.enrichment_families
            if aliases.get(configured, configured) in EVIDENCE_FAMILIES
        )
    )


def routing_questions(_check: Check, families: tuple[str, ...]) -> dict[str, Question]:
    questions: dict[str, Question] = {
        "disposition": ChoiceQuestion(
            type="choice",
            instructions=(
                "Which evidence disposition applies to this rule and exact target? First determine whether "
                "the operation is applicable, then whether concrete necessary evidence is missing. "
                "Do not infer missing facts merely from low model confidence. Source context sufficiency "
                "is distinct from certainty about the verdict."
            ),
            criteria=DISPOSITIONS,
        )
    }
    for family in families:
        description = EVIDENCE_FAMILIES[family]
        questions[family] = NoulQuestion(
            type="noul",
            instructions=(
                "Assuming the rule applies and additional local evidence could help, would this evidence "
                f"family supply a concrete currently missing fact for the exact target: {family}: {description} "
                "Judge this family independently: several families or none may help. Evidence already present, "
                "a matching short name alone, or generic extra context is insufficient. "
                "Do not assume any other question's answer or prefer evidence that supports a defect."
            ),
        )
    return questions


def routing_decision(
    answers: dict[str, Answer],
    families: tuple[str, ...],
    *,
    min_route_confidence: float,
    min_route_probability: float,
    min_evidence_probability: float,
) -> Routing:
    disposition = answers["disposition"]
    assert isinstance(disposition, ChoiceAnswer)
    scores = {}
    for name in families:
        answer = answers[name]
        assert isinstance(answer, NoulAnswer)
        scores[name] = answer.noul
    if (
        disposition.confidence < min_route_confidence
        or disposition.probabilities[disposition.choice] < min_route_probability
    ):
        return Routing(None)
    if disposition.choice != "local_evidence":
        return Routing(disposition.choice)
    return Routing(
        disposition.choice,
        tuple(
            name
            for name in sorted(scores, key=lambda name: (-scores[name], name))
            if scores[name] >= min_evidence_probability
        ),
    )

