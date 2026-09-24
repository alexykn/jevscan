"""One evidence-driven refinement pass: route, select candidates, reassess.

Jev chooses only among bounded options. Code owns discovery, snapshots, budgets,
cache use and termination; no model-generated paths or executable actions exist.
"""

import hashlib
import json
from dataclasses import dataclass
from itertools import zip_longest
from typing import Any

from jevscan.core.assessment import Assessment
from jevscan.core.config import EnrichmentConfig
from jevscan.core.context import ContextBuilder, Evidence
from jevscan.core.inference import Inference, Prediction
from jevscan.core.planning import RequestBudget
from jevscan.core.protocol import Answer, Check, ChoiceAnswer, ContextLimitError, NoulAnswer, PromptRegistry, encode
from jevscan.core.retrieval import Candidate, SourceIndex
from jevscan.core.rules import ChoiceQuestion, EnrichmentTrigger, NoulQuestion, Question
from jevscan.core.selection import rank_candidates

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


class EnrichmentStoppedError(Exception):
    """An explicit local limit, not a transport failure or a negative finding."""


@dataclass(frozen=True, slots=True)
class Reassessment:
    prediction: Prediction
    evidence: Evidence
    wire_state: bytes
    question_wire: dict[str, Any]


def _augment(
    context: ContextBuilder, initial: Evidence, candidates: list[Candidate], retrieval: dict[str, Any]
) -> Evidence:
    """Union overlapping source spans, preserving every byte of the original evidence."""
    sources = {context.parsed.path: context.parsed.source}
    ranges: dict[str, list[tuple[int, int]]] = {}
    languages = {}
    for document in initial.state["documents"]:
        path = document["path"]
        languages[path] = document["language"]
        ranges.setdefault(path, []).append((document["start_byte"], document["end_byte"]))
    for candidate in candidates:
        target = candidate.target
        sources[target.path] = candidate.snapshot.parsed.source
        languages[target.path] = target.language
        ranges.setdefault(target.path, []).append((target.start_byte, target.end_byte))
    documents = []
    for path, spans in sorted(ranges.items()):
        merged: list[tuple[int, int]] = []
        for start, end in sorted(spans):
            if merged and start <= merged[-1][1]:
                merged[-1] = merged[-1][0], max(end, merged[-1][1])
            else:
                merged.append((start, end))
        source = sources[path]
        for start, end in merged:
            documents.append({
                "path": path,
                "language": languages[path],
                "start_byte": start,
                "end_byte": end,
                "start_line": source.count(b"\n", 0, start) + 1,
                "end_line": source.count(b"\n", 0, max(start, end - 1)) + 1,
                "content": source[start:end].decode("utf-8"),
                "file_sha256": hashlib.sha256(source).hexdigest(),
            })
    state = {
        **initial.state,
        "documents": documents,
        "coverage": {
            **context.coverage(documents),
            "external_references": "Selected syntax/name-based candidates only; no complete or resolved call graph. Sources are per-file snapshots, not an atomic repository snapshot.",
        },
        "supplemental_evidence": [candidate.model_metadata() for candidate in candidates],
        "retrieval_coverage": retrieval,
    }
    return Evidence(state, encode(state))


class Enricher:
    def __init__(
        self,
        context: ContextBuilder,
        budget: RequestBudget,
        limits: EnrichmentConfig,
        index: SourceIndex,
        inference: Inference,
        rubric: PromptRegistry,
    ) -> None:
        self.context, self.budget, self.limits = context, budget, limits
        self.index, self.inference = index, inference
        self.rubric = rubric
        self.calls = 0
        self.reviewed = 0
        self._snapshots: dict[str, str] = {context.parsed.path: context.parsed.source.decode("utf-8")}

    def source_documents(self) -> dict[str, str]:
        return dict(self._snapshots)

    @staticmethod
    def _wire(check: Check, questions: dict[str, Question]) -> dict[str, bytes]:
        return {key: encode(check.auxiliary(question)) for key, question in questions.items()}

    async def _predict(
        self, phase: str, state: bytes, questions: dict[str, Question], wire: dict[str, bytes], trace: dict[str, Any]
    ) -> Prediction:
        if self.calls >= self.limits.max_calls_per_file:
            raise EnrichmentStoppedError("call_budget")
        if not self.budget.fits(state, wire):
            raise EnrichmentStoppedError("request_budget")
        self.calls += 1
        body = self.budget.body(state, wire)
        entry: dict[str, Any] = {
            "phase": phase,
            "request_sha256": hashlib.sha256(body).hexdigest(),
            "question_wires": {key: json.loads(value) for key, value in wire.items()},
        }
        trace["predictions"].append(entry)
        prediction = await self.inference.predict(
            body,
            questions,
            enrichment=True,
            state_bytes=len(state),
            question_bytes=sum(map(len, wire.values())),
            state=json.loads(state),
        )
        entry.update({
            "model": prediction.response.model,
            "cached": prediction.cached,
            "metrics": prediction.metrics,
            "answers": {key: answer.model_dump(mode="json") for key, answer in prediction.response.answers.items()},
        })
        return prediction

    def _allowed_families(self, check: Check) -> tuple[str, ...]:
        return allowed_families(check, self.limits)

    def _routing_questions(self, check: Check) -> dict[str, Question]:
        return routing_questions(check, self._allowed_families(check))

    async def _routing_answers(self, check: Check, evidence: Evidence, trace: dict[str, Any]) -> dict[str, Answer]:
        """Batch independent routing questions; small configured request budgets still apply."""
        questions = self._routing_questions(check)
        wire = self._wire(check, questions)
        rubric = PromptRegistry.for_question(check.rule.question)
        state = rubric.state_bytes(evidence.state)
        pending: dict[str, Question] = {}
        answers: dict[str, Answer] = {}
        for name, question in questions.items():
            proposed = {key: wire[key] for key in (*pending, name)}
            if pending and not self.budget.fits(state, proposed):
                prediction = await self._predict("route", state, pending, {key: wire[key] for key in pending}, trace)
                answers.update(prediction.response.answers)
                pending = {}
            pending[name] = question
        if pending:
            prediction = await self._predict("route", state, pending, {key: wire[key] for key in pending}, trace)
            answers.update(prediction.response.answers)
        return answers

    async def _route(self, check: Check, evidence: Evidence, trace: dict[str, Any]) -> Routing:
        answers = await self._routing_answers(check, evidence, trace)
        families = self._allowed_families(check)
        trace["routing_config"] = {
            "allowed_families": list(families),
            "min_route_confidence": self.limits.min_route_confidence,
            "min_route_probability": self.limits.min_route_probability,
            "min_evidence_probability": self.limits.min_evidence_probability,
        }
        decision = routing_decision(
            answers,
            families,
            min_route_confidence=self.limits.min_route_confidence,
            min_route_probability=self.limits.min_route_probability,
            min_evidence_probability=self.limits.min_evidence_probability,
        )
        probabilities = {}
        for name in families:
            answer = answers[name]
            if isinstance(answer, NoulAnswer):
                probabilities[name] = answer.noul
        trace["evidence_probabilities"] = probabilities
        return decision

    async def _candidates(
        self, check: Check, evidence: Evidence, families: tuple[str, ...], trace: dict[str, Any]
    ) -> list[Candidate]:
        """Pool families fairly under one candidate limit, retaining all family provenance."""
        pools = {}
        for family in families:
            pools[family] = await self.index.candidates(self.context, check, evidence, family)
        membership: dict[str, list[str]] = {}
        for family, pool in pools.items():
            for candidate in pool.items:
                membership.setdefault(candidate.id, []).append(family)
        unique: dict[str, Candidate] = {}
        for row in zip_longest(*(pool.items for pool in pools.values())):
            for candidate in row:
                if candidate is not None:
                    unique.setdefault(candidate.id, candidate)
        admitted = list(unique.values())[: self.limits.max_candidates]
        trace["retrieval"] = {
            "families": {family: pool.coverage for family, pool in pools.items()},
            "candidate_families": membership,
            "pooled_candidates": len(unique),
            "combined_candidate_limit_omissions": max(0, len(unique) - len(admitted)),
        }
        return admitted

    async def _select(
        self, check: Check, evidence: Evidence, families: tuple[str, ...], trace: dict[str, Any]
    ) -> list[Candidate]:
        if self.calls >= self.limits.max_calls_per_file:
            raise EnrichmentStoppedError("call_budget")
        candidates = await self._candidates(check, evidence, families, trace)
        ranked = await rank_candidates(
            check,
            evidence,
            candidates,
            self.budget,
            self._predict,
            trace,
            PromptRegistry.for_question(check.rule.question),
        )
        ranked = [pair for pair in ranked if pair[0] >= self.limits.min_relevance]
        selected: list[Candidate] = []
        wire = {check.id: encode(check.question())}
        registry = self.rubric
        for _, candidate in ranked:
            proposed = [*selected, candidate]
            if len(proposed) <= self.limits.max_evidence and self.budget.fits(
                registry.state_bytes(_augment(self.context, evidence, proposed, trace["retrieval"]).state), wire
            ):
                selected.append(candidate)
            else:
                trace["omitted_candidates"].append({"id": candidate.id, "reason": "evidence_budget"})
        if not selected and trace["omitted_candidates"]:
            raise EnrichmentStoppedError("evidence_budget")
        return selected

    async def refine(
        self, check: Check, answer: Answer, evidence: Evidence, trace: dict[str, Any]
    ) -> Reassessment | None:
        trace.update({
            "initial_answer": answer.model_dump(mode="json"),
            "initial_state_sha256": hashlib.sha256(evidence.encoded).hexdigest(),
            "predictions": [],
            "candidates": [],
            "omitted_candidates": [],
            "selected": [],
        })
        if self.reviewed >= self.limits.max_checks_per_file:
            trace["outcome"] = "check_budget"
            return None
        self.reviewed += 1
        self.inference.summary.enrichment_reviewed += 1
        trace["outcome"] = "failed"  # Retained if a genuine service/validation failure aborts the scan.
        try:
            allowed_families = self._allowed_families(check)
            if not allowed_families:
                trace["outcome"] = "enrichment_disabled"
                return None
            policy = check.rule.targeted_enrichment
            declared = policy is not None and (
                (isinstance(answer, ChoiceAnswer) and answer.choice in policy.when_choices)
                or (trace.get("trigger") in policy.when_reasons)
            )
            if declared:
                routing = Routing("local_evidence", allowed_families)
                trace["routing_mode"] = "declared_rule_families"
            else:
                routing = await self._route(check, evidence, trace)
            trace["disposition"] = routing.disposition
            trace["evidence_families"] = list(routing.families)
            if routing.disposition is None:
                trace["outcome"] = "route_uncertain"
                return None
            if routing.disposition != "local_evidence":
                trace["outcome"] = routing.disposition
                return None
            if not routing.families:
                trace["outcome"] = "no_evidence_family"
                return None
            selected = await self._select(check, evidence, routing.families, trace)
            if not selected:
                trace["outcome"] = "no_relevant_evidence"
                return None
            enriched = _augment(self.context, evidence, selected, trace["retrieval"])
            request_state = self.rubric.state_bytes(enriched.state)
            question_wire = check.question()
            self._snapshots.update({
                candidate.snapshot.parsed.path: candidate.snapshot.parsed.source.decode("utf-8")
                for candidate in selected
            })
            trace["selected"] = [candidate.metadata() for candidate in selected]
            # Fresh judgment: no initial answer, route, relevance score, or expected label in state.
            prediction = await self._predict(
                "reassess",
                request_state,
                {check.id: check.rule.question},
                {check.id: encode(question_wire)},
                trace,
            )
            self.inference.summary.enrichment_reruns += 1
            trace["outcome"] = "reassessed"
            return Reassessment(prediction, enriched, request_state, question_wire)
        except EnrichmentStoppedError as exc:
            trace["outcome"] = str(exc)
        except ContextLimitError:
            trace["outcome"] = "provider_context_limit"
        return None
