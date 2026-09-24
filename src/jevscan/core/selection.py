"""Shared rule-derived candidate questions for enrichment and context compaction."""

from collections.abc import Awaitable, Callable
from typing import Any

from jevscan.core.context import Evidence
from jevscan.core.inference import Prediction
from jevscan.core.planning import RequestBudget
from jevscan.core.protocol import Check, NoulAnswer, PromptRegistry, encode
from jevscan.core.retrieval import Candidate
from jevscan.core.rules import NoulQuestion, Question

Predict = Callable[[str, bytes, dict[str, Question], dict[str, bytes], dict[str, Any]], Awaitable[Prediction]]


def selection_input(
    check: Check, evidence: Evidence, candidates: list[Candidate], rubric: PromptRegistry
) -> tuple[bytes, dict[str, Question], dict[str, bytes]]:
    questions: dict[str, Question] = {
        candidate.id: NoulQuestion(
            type="noul",
            instructions=(
                f"Would the complete source for candidate `{candidate.id}` in `candidate_context` provide concrete "
                "evidence useful for deciding the supplied rule, its instructions and criteria, about this exact target? "
                "Judge relevance, not whether it supports a positive or negative verdict. A shared short name alone, "
                "unrelated tests, or evidence already supplied is insufficient. The preview may be partial; "
                "do not invent omitted source. Independent candidates can both be relevant."
            ),
        )
        for candidate in candidates
    }
    rubric.validate_questions((check.rule.question,))
    state = rubric.state_bytes({**evidence.state, "candidate_context": [item.preview() for item in candidates]})
    return (
        state,
        questions,
        {name: encode(check.auxiliary(question)) for name, question in questions.items()},
    )


async def rank_candidates(
    check: Check,
    evidence: Evidence,
    candidates: list[Candidate],
    budget: RequestBudget,
    predict: Predict,
    trace: dict[str, Any],
    rubric: PromptRegistry,
) -> list[tuple[float, Candidate]]:
    """Pack independent questions without weakening any caller's request or call limits."""
    ranked: list[tuple[float, Candidate]] = []

    async def flush(batch: list[Candidate]) -> None:
        state, questions, wire = selection_input(check, evidence, batch, rubric)
        prediction = await predict("selection", state, questions, wire, trace)
        for candidate in batch:
            answer = prediction.response.answers[candidate.id]
            assert isinstance(answer, NoulAnswer)
            trace["candidates"].append({**candidate.metadata(), "relevance": answer.noul})
            ranked.append((answer.noul, candidate))

    pending: list[Candidate] = []
    for candidate in candidates:
        state, _, wire = selection_input(check, evidence, [*pending, candidate], rubric)
        if pending and not budget.fits(state, wire):
            await flush(pending)
            pending = []
        state, _, wire = selection_input(check, evidence, [candidate], rubric)
        if budget.fits(state, wire):
            pending.append(candidate)
        else:
            trace["omitted_candidates"].append({"id": candidate.id, "reason": "selection_budget"})
    if pending:
        await flush(pending)
    return sorted(ranked, key=lambda pair: (-pair[0], pair[1].target.path, pair[1].target.start_byte))
