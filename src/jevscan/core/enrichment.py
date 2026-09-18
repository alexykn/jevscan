"""One evidence-driven refinement pass: route, select candidates, reassess.

Jev chooses only among bounded options. Code owns discovery, snapshots, budgets,
cache use and termination; no model-generated paths or executable actions exist.
"""

import hashlib
from dataclasses import dataclass
from typing import Any

from jevscan.core.config import EnrichmentConfig
from jevscan.core.context import ContextBuilder, Evidence
from jevscan.core.inference import Inference, Prediction
from jevscan.core.planning import RequestBudget
from jevscan.core.protocol import QUESTION_POLICY, Answer, Check, ChoiceAnswer, ContextLimitError, NoulAnswer, encode
from jevscan.core.retrieval import Candidate, SourceIndex
from jevscan.core.rules import ChoiceQuestion, NoulQuestion, Question

ROUTES = {
    "not_applicable": "The target does not exhibit the kind of operation this rule evaluates; more callers would not make the rule applicable.",
    "sufficient": "The supplied implementation and context are sufficient for this rule; any remaining uncertainty is interpretation or rubric ambiguity, not a concrete missing fact.",
    "callers": "Actual uses of the target are missing and could establish how its inputs, results, or lifecycle are constrained.",
    "definitions": "Implementations or type/contract definitions referenced by the target are missing and could resolve the judgment.",
    "tests": "Concrete tests of this target are missing and could clarify intended behavior; tests alone cannot prove universal guarantees.",
    "enclosing_context": "The surrounding owner or file implementation is missing and is needed to judge this target.",
    "unavailable": "The missing fact is a runtime condition, external requirement, or unresolved contract that local source retrieval is unlikely to establish.",
}


class EnrichmentStoppedError(Exception):
    """An explicit local limit, not a transport failure or a negative finding."""


@dataclass(frozen=True, slots=True)
class Reassessment:
    prediction: Prediction
    evidence: Evidence


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
        "supplemental_evidence": [candidate.metadata() for candidate in candidates],
        "retrieval_coverage": retrieval,
    }
    return Evidence(initial.start, initial.end, state, encode(state))


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

    @staticmethod
    def _wire(check: Check, questions: dict[str, Question]) -> dict[str, bytes]:
        return {
            key: encode({
                **question.model_dump(mode="json"),
                "instructions": {
                    "policy": QUESTION_POLICY,
                    "target": check.target.metadata(),
                    "rule": check.rule.question.model_dump(mode="json"),
                    "task": question.instructions,
                },
            })
            for key, question in questions.items()
        }

    async def _predict(
        self, phase: str, state: bytes, questions: dict[str, Question], wire: dict[str, bytes], trace: dict[str, Any]
    ) -> Prediction:
        if self.calls >= self.limits.max_calls_per_file:
            raise EnrichmentStoppedError("call_budget")
        if not self.budget.fits(state, wire):
            raise EnrichmentStoppedError("request_budget")
        self.calls += 1
        body = self.budget.body(state, wire)
        entry: dict[str, Any] = {"phase": phase, "request_sha256": hashlib.sha256(body).hexdigest()}
        trace["predictions"].append(entry)
        prediction = await self.inference.predict(body, questions, enrichment=True)
        entry.update({
            "model": prediction.response.model,
            "cached": prediction.cached,
            "answers": {key: answer.model_dump(mode="json") for key, answer in prediction.response.answers.items()},
        })
        return prediction

    async def _route(self, check: Check, evidence: Evidence, trace: dict[str, Any]) -> str | None:
        questions: dict[str, Question] = {
            "route": ChoiceQuestion(
                type="choice",
                instructions=(
                    "Which single next step would most help decide the supplied rule about this target? "
                    "Inspect the actual documents and coverage, not assumptions about unseen code. "
                    "Choose sufficient when the needed evidence is already present. "
                    "Do not choose callers merely because the target is a function. "
                    "This is an evidence-routing judgment, not a diagnosis of the model's internal reasoning."
                ),
                criteria=ROUTES,
            )
        }
        prediction = await self._predict("route", evidence.encoded, questions, self._wire(check, questions), trace)
        answer = prediction.response.answers["route"]
        assert isinstance(answer, ChoiceAnswer)
        if (
            answer.confidence < self.limits.min_route_confidence
            or answer.probabilities[answer.choice] < self.limits.min_route_probability
        ):
            return None
        return answer.choice

    def _selection_input(
        self, check: Check, evidence: Evidence, candidates: list[Candidate]
    ) -> tuple[bytes, dict[str, Question], dict[str, bytes]]:
        questions: dict[str, Question] = {
            candidate.id: NoulQuestion(
                type="noul",
                instructions=(
                    f"Would the complete source for candidate `{candidate.id}` in `candidate_context` supply "
                    "concrete, currently missing evidence for deciding this rule about this exact target? "
                    "Judge relevance, not whether it supports a positive or negative verdict. "
                    "A shared short name alone, unrelated tests, or evidence already supplied is insufficient. "
                    "The preview may be partial; do not invent the omitted contents."
                ),
            )
            for candidate in candidates
        }
        state = encode({**evidence.state, "candidate_context": [candidate.preview() for candidate in candidates]})
        return state, questions, self._wire(check, questions)

    async def _rank_batch(
        self, check: Check, evidence: Evidence, candidates: list[Candidate], trace: dict[str, Any]
    ) -> list[tuple[float, Candidate]]:
        state, questions, wire = self._selection_input(check, evidence, candidates)
        prediction = await self._predict("selection", state, questions, wire, trace)
        ranked = []
        for candidate in candidates:
            answer = prediction.response.answers[candidate.id]
            assert isinstance(answer, NoulAnswer)
            trace["candidates"].append({**candidate.metadata(), "relevance": answer.noul})
            if answer.noul >= self.limits.min_relevance:
                ranked.append((answer.noul, candidate))
        return ranked

    async def _select(self, check: Check, evidence: Evidence, route: str, trace: dict[str, Any]) -> list[Candidate]:
        if self.calls >= self.limits.max_calls_per_file:
            raise EnrichmentStoppedError("call_budget")
        found = await self.index.candidates(self.context, check, evidence, route)
        trace["retrieval"] = found.coverage
        ranked = []
        pending: list[Candidate] = []
        for candidate in found.items:
            state, _, wire = self._selection_input(check, evidence, [*pending, candidate])
            if pending and not self.budget.fits(state, wire):
                ranked.extend(await self._rank_batch(check, evidence, pending, trace))
                pending = []
            state, _, wire = self._selection_input(check, evidence, [candidate])
            if self.budget.fits(state, wire):
                pending.append(candidate)
            else:
                trace["omitted_candidates"].append({"id": candidate.id, "reason": "selection_budget"})
        if pending:
            ranked.extend(await self._rank_batch(check, evidence, pending, trace))
        ranked.sort(key=lambda pair: (-pair[0], pair[1].target.path, pair[1].target.start_byte))
        selected: list[Candidate] = []
        wire = {check.id: encode(check.question())}
        for _, candidate in ranked:
            proposed = [*selected, candidate]
            if len(proposed) <= self.limits.max_evidence and self.budget.fits(
                _augment(self.context, evidence, proposed, trace["retrieval"]).encoded, wire
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
            route = await self._route(check, evidence, trace)
            trace["route"] = route
            if route is None:
                trace["outcome"] = "route_uncertain"
                return None
            if route in {"not_applicable", "sufficient", "unavailable"}:
                trace["outcome"] = route
                return None
            selected = await self._select(check, evidence, route, trace)
            if not selected:
                trace["outcome"] = "no_relevant_evidence"
                return None
            enriched = _augment(self.context, evidence, selected, trace["retrieval"])
            trace["selected"] = [candidate.metadata() for candidate in selected]
            # Fresh judgment: no initial answer, route, relevance score, or expected label in state.
            prediction = await self._predict(
                "reassess",
                enriched.encoded,
                {check.id: check.rule.question},
                {check.id: encode(check.question())},
                trace,
            )
            self.inference.summary.enrichment_reruns += 1
            trace["outcome"] = "reassessed"
            return Reassessment(prediction, enriched)
        except EnrichmentStoppedError as exc:
            trace["outcome"] = str(exc)
        except ContextLimitError:
            trace["outcome"] = "provider_context_limit"
        return None
