"""One evidence-driven refinement pass: route, select candidates, reassess.

Jev chooses only among bounded options. Code owns discovery, snapshots, budgets,
cache use and termination; no model-generated paths or executable actions exist.
"""

import hashlib
import json
from dataclasses import dataclass
from itertools import zip_longest
from typing import Any

from jevscan.core.config import EnrichmentConfig
from jevscan.core.context import ContextBuilder, Evidence
from jevscan.core.enrichment_routing import Routing, allowed_families, routing_decision, routing_questions
from jevscan.core.evidence_merge import augment_evidence
from jevscan.core.inference import Inference, Prediction
from jevscan.core.planning import RequestBudget
from jevscan.core.protocol import Answer, Check, ChoiceAnswer, ContextLimitError, NoulAnswer, PromptRegistry, encode
from jevscan.core.retrieval import Candidate, SourceIndex
from jevscan.core.rules import Question
from jevscan.core.selection import rank_candidates


class EnrichmentStoppedError(Exception):
    """An explicit local limit, not a transport failure or a negative finding."""


@dataclass(frozen=True, slots=True)
class Reassessment:
    prediction: Prediction
    evidence: Evidence
    wire_state: bytes
    question_wire: dict[str, Any]


class Enricher:
    def __init__(
        self,
        context: ContextBuilder,
        budget: RequestBudget,
        limits: EnrichmentConfig,
        index: SourceIndex,
        inference: Inference,
    ) -> None:
        self.context, self.budget, self.limits = context, budget, limits
        self.index, self.inference = index, inference
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

    def _routing_batches(
        self,
        state: bytes,
        questions: dict[str, Question],
        wire: dict[str, bytes],
    ) -> list[dict[str, Question]]:
        batches: list[dict[str, Question]] = []
        pending: dict[str, Question] = {}
        for name, question in questions.items():
            proposed_names = (*pending, name)
            proposed = {key: wire[key] for key in proposed_names}
            if pending and not self.budget.fits(state, proposed):
                batches.append(pending)
                pending = {}
            pending[name] = question
        if pending:
            batches.append(pending)
        return batches

    async def _routing_answers(self, check: Check, evidence: Evidence, trace: dict[str, Any]) -> dict[str, Answer]:
        """Batch independent routing questions; small configured request budgets still apply."""
        questions = self._routing_questions(check)
        wire = self._wire(check, questions)
        rubric = PromptRegistry.for_question(check.rule.question)
        rubric.validate_questions((check.rule.question,))
        state = rubric.state_bytes(evidence.state)
        answers: dict[str, Answer] = {}
        for batch in self._routing_batches(state, questions, wire):
            batch_wire = {key: wire[key] for key in batch}
            prediction = await self._predict("route", state, batch, batch_wire, trace)
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

    async def _candidate_pools(
        self,
        check: Check,
        evidence: Evidence,
        families: tuple[str, ...],
    ) -> dict[str, Any]:
        return {family: await self.index.candidates(self.context, check, evidence, family) for family in families}

    @staticmethod
    def _candidate_membership(pools: dict[str, Any]) -> dict[str, list[str]]:
        membership: dict[str, list[str]] = {}
        for family, pool in pools.items():
            for candidate in pool.items:
                membership.setdefault(candidate.id, []).append(family)
        return membership

    @staticmethod
    def _fair_candidates(pools: dict[str, Any]) -> list[Candidate]:
        unique: dict[str, Candidate] = {}
        for row in zip_longest(*(pool.items for pool in pools.values())):
            for candidate in row:
                if candidate is not None:
                    unique.setdefault(candidate.id, candidate)
        return list(unique.values())

    async def _candidates(
        self,
        check: Check,
        evidence: Evidence,
        families: tuple[str, ...],
        trace: dict[str, Any],
    ) -> list[Candidate]:
        """Pool families fairly under one candidate limit, retaining all family provenance."""
        pools = await self._candidate_pools(check, evidence, families)
        pooled = self._fair_candidates(pools)
        admitted = pooled[: self.limits.max_candidates]
        trace["retrieval"] = {
            "families": {family: pool.coverage for family, pool in pools.items()},
            "candidate_families": self._candidate_membership(pools),
            "pooled_candidates": len(pooled),
            "combined_candidate_limit_omissions": max(0, len(pooled) - len(admitted)),
        }
        return admitted

    def _candidate_fits(
        self,
        check: Check,
        evidence: Evidence,
        selected: list[Candidate],
        candidate: Candidate,
        registry: PromptRegistry,
        trace: dict[str, Any],
    ) -> bool:
        proposed = [*selected, candidate]
        if len(proposed) > self.limits.max_evidence:
            return False
        enriched = augment_evidence(self.context, evidence, proposed, trace["retrieval"])
        state = registry.state_bytes(enriched.state)
        wire = {check.id: encode(check.question())}
        return self.budget.fits(state, wire)

    def _admit_ranked(
        self,
        check: Check,
        evidence: Evidence,
        ranked: list[tuple[float, Candidate]],
        registry: PromptRegistry,
        trace: dict[str, Any],
    ) -> list[Candidate]:
        selected: list[Candidate] = []
        for _, candidate in ranked:
            if self._candidate_fits(check, evidence, selected, candidate, registry, trace):
                selected.append(candidate)
            else:
                trace["omitted_candidates"].append({"id": candidate.id, "reason": "evidence_budget"})
        return selected

    async def _select(
        self,
        check: Check,
        evidence: Evidence,
        families: tuple[str, ...],
        trace: dict[str, Any],
    ) -> list[Candidate]:
        if self.calls >= self.limits.max_calls_per_file:
            raise EnrichmentStoppedError("call_budget")
        candidates = await self._candidates(check, evidence, families, trace)
        registry = PromptRegistry.for_question(check.rule.question)
        registry.validate_questions((check.rule.question,))
        ranked = await rank_candidates(
            check,
            evidence,
            candidates,
            self.budget,
            self._predict,
            trace,
            registry,
        )
        relevant = [pair for pair in ranked if pair[0] >= self.limits.min_relevance]
        selected = self._admit_ranked(check, evidence, relevant, registry, trace)
        if not selected and trace["omitted_candidates"]:
            raise EnrichmentStoppedError("evidence_budget")
        return selected

    def _start_review(self, answer: Answer, evidence: Evidence, trace: dict[str, Any]) -> bool:
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
            return False
        self.reviewed += 1
        self.inference.summary.enrichment_reviewed += 1
        trace["outcome"] = "failed"
        return True

    def _declared_routing(
        self,
        check: Check,
        answer: Answer,
        allowed: tuple[str, ...],
        trace: dict[str, Any],
    ) -> Routing | None:
        policy = check.rule.targeted_enrichment
        if policy is None:
            return None
        choice_trigger = isinstance(answer, ChoiceAnswer) and answer.choice in policy.when_choices
        reason_trigger = trace.get("trigger") in policy.when_reasons
        if not (choice_trigger or reason_trigger):
            return None
        trace["routing_mode"] = "declared_rule_families"
        return Routing("local_evidence", allowed)

    @staticmethod
    def _routing_outcome(routing: Routing) -> str | None:
        if routing.disposition is None:
            return "route_uncertain"
        if routing.disposition != "local_evidence":
            return routing.disposition
        if not routing.families:
            return "no_evidence_family"
        return None

    async def _review_route(
        self,
        check: Check,
        answer: Answer,
        evidence: Evidence,
        trace: dict[str, Any],
    ) -> Routing | None:
        allowed = self._allowed_families(check)
        if not allowed:
            trace["outcome"] = "enrichment_disabled"
            return None

        routing = self._declared_routing(check, answer, allowed, trace)
        if routing is None:
            routing = await self._route(check, evidence, trace)

        trace["disposition"] = routing.disposition
        trace["evidence_families"] = list(routing.families)
        outcome = self._routing_outcome(routing)
        if outcome is not None:
            trace["outcome"] = outcome
            return None
        return routing

    async def _reassess(
        self,
        check: Check,
        evidence: Evidence,
        selected: list[Candidate],
        trace: dict[str, Any],
    ) -> Reassessment:
        enriched = augment_evidence(self.context, evidence, selected, trace["retrieval"])
        registry = PromptRegistry.for_question(check.rule.question)
        registry.validate_questions((check.rule.question,))
        request_state = registry.state_bytes(enriched.state)
        question_wire = check.question()
        self._snapshots.update({
            candidate.snapshot.parsed.path: candidate.snapshot.parsed.source.decode("utf-8") for candidate in selected
        })
        trace["selected"] = [candidate.metadata() for candidate in selected]
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

    async def refine(
        self, check: Check, answer: Answer, evidence: Evidence, trace: dict[str, Any]
    ) -> Reassessment | None:
        if not self._start_review(answer, evidence, trace):
            return None
        try:
            routing = await self._review_route(check, answer, evidence, trace)
            if routing is None:
                return None
            selected = await self._select(check, evidence, routing.families, trace)
            if not selected:
                trace["outcome"] = "no_relevant_evidence"
                return None
            return await self._reassess(check, evidence, selected, trace)
        except EnrichmentStoppedError as exc:
            trace["outcome"] = str(exc)
        except ContextLimitError:
            trace["outcome"] = "provider_context_limit"
        return None
